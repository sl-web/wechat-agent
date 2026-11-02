"""批量废弃/删除 测试。

口径(必须与前端文案一致):
- 文章 废弃 = status→rejected(**可恢复**,数据仍在)
- 文章 删除 = 物理删行,**仅未发布**;已发布/已回流必须跳过并带上原因
- 热点 废弃 = hot_topics.deleted=1(软删)+ kb_clean_logs 审计

为什么要测:批量操作最容易出的问题是"整批失败"和"悄悄跳过了却不告诉用户"。
所以这里既断言成功路径,也断言 skipped 的**原因文本**不是空的。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.db import connect, init_db, query
from app.generator.templates import seed_default_template
from app.knowledge.cleaner import BULK_LIMIT as HOT_LIMIT
from app.knowledge.cleaner import bulk_soft_delete
from app.review.service import BULK_LIMIT, bulk_clean, bulk_discard


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    seed_default_template(str(p))

    conn = connect(p)
    tid = conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date, deleted)
           VALUES('rss','rss','热点甲','http://t/1','2026-09-10',0)"""
    ).lastrowid
    tid2 = conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date, deleted)
           VALUES('rss','rss','热点乙','http://t/2','2026-09-10',0)"""
    ).lastrowid
    ids = {}
    for name, status in (("已生成稿", "generated"), ("已发布稿", "published"),
                         ("已推送稿", "pushed"), ("已回流稿", "backfilled"),
                         # 用户在界面上最先想清掉的就是这两类:生成失败(熔断)与已推送待发布
                         ("生成失败稿", "generation_failed"), ("已驳回稿", "rejected")):
        ids[name] = conn.execute(
            """INSERT INTO articles(topic_id, template_id, structure_id, title, body, status)
               VALUES(?,?,?,?,?,?)""",
            (tid, "tpl_ai_news_v1", "s1", name, "正文" * 50, status),
        ).lastrowid
    conn.commit()
    conn.close()
    return {"path": str(p), "topic": tid, "topic2": tid2, "articles": ids}


def _status(db, aid: int) -> str:
    conn = connect(db)
    try:
        rows = query(conn, "SELECT status FROM articles WHERE id=?", (aid,))
        return rows[0]["status"] if rows else "<不存在>"
    finally:
        conn.close()


# ---------------- 文章:废弃(可恢复) ----------------

def test_bulk_discard_marks_rejected_and_keeps_rows(db_path):
    p = db_path["path"]
    a = db_path["articles"]
    r = bulk_discard([a["已生成稿"], a["已推送稿"]], reason="人工废弃", db_path=p)

    assert r["ok"] is True and r["action"] == "discard"
    assert r["done_count"] == 2 and r["skipped_count"] == 0
    # 状态变了,但**行还在**(废弃可恢复,不是删除)
    assert _status(p, a["已生成稿"]) == "rejected"
    assert _status(p, a["已推送稿"]) == "rejected"
    conn = connect(p)
    try:
        rows = query(conn, "SELECT reject_reason FROM articles WHERE id=?", (a["已生成稿"],))
        assert rows[0]["reject_reason"] == "人工废弃"
    finally:
        conn.close()


def test_bulk_discard_works_on_published_too(db_path):
    """废弃只是改状态,已发布稿件也不该拒绝(它不破坏数据)。"""
    p = db_path["path"]
    a = db_path["articles"]
    r = bulk_discard([a["已发布稿"]], db_path=p)
    assert r["done_count"] == 1 and r["skipped_count"] == 0


# ---------------- 文章:物理删除(有守卫) ----------------

def test_bulk_clean_deletes_unpublished_and_skips_published(db_path):
    p = db_path["path"]
    a = db_path["articles"]
    r = bulk_clean([a["已生成稿"], a["已发布稿"], a["已回流稿"]], db_path=p)

    assert r["done_count"] == 1 and r["skipped_count"] == 2
    assert _status(p, a["已生成稿"]) == "<不存在>"      # 未发布 → 真删了
    assert _status(p, a["已发布稿"]) == "published"     # 已发布 → 必须原样保留
    assert _status(p, a["已回流稿"]) == "backfilled"

    # 跳过必须**带原因**,不能只回一个数字(否则 UI 无法解释"为什么少删了几条")
    reasons = {s["id"]: s["reason"] for s in r["skipped"]}
    assert "已发布" in reasons[a["已发布稿"]]
    assert "已发布" in reasons[a["已回流稿"]]


def test_bulk_clean_missing_id_is_skipped_with_reason(db_path):
    p = db_path["path"]
    r = bulk_clean([999999], db_path=p)
    assert r["done_count"] == 0 and r["skipped_count"] == 1
    assert "不存在" in r["skipped"][0]["reason"]


def test_bulk_clean_removes_generation_failed_pushed_and_rejected(db_path):
    """生成失败 / 已推送 / 已驳回 都是未发布状态,必须能删掉。

    这条对应用户最常见的操作:清理「生成失败/熔断」积压。
    只有 published/backfilled 才该被拦住 —— 别把守卫扩大到未发布状态上。
    """
    p = db_path["path"]
    a = db_path["articles"]
    r = bulk_clean([a["生成失败稿"], a["已推送稿"], a["已驳回稿"]], db_path=p)
    assert r["skipped_count"] == 0, f"不该跳过:{r['skipped']}"
    assert r["done_count"] == 3
    for k in ("生成失败稿", "已推送稿", "已驳回稿"):
        assert _status(p, a[k]) == "<不存在>"


def test_bulk_ids_are_deduped_and_junk_dropped(db_path):
    p = db_path["path"]
    a = db_path["articles"]
    aid = a["已生成稿"]
    r = bulk_discard([aid, aid, "abc", None, str(aid)], db_path=p)
    assert r["done_count"] == 1                      # 重复只算一次
    assert r["skipped_count"] == 0                   # 非整数被丢弃而不是报错


def test_bulk_over_limit_raises(db_path):
    p = db_path["path"]
    with pytest.raises(ValueError, match=str(BULK_LIMIT)):
        bulk_clean(list(range(1, BULK_LIMIT + 2)), db_path=p)


def test_bulk_clean_does_not_abort_batch_on_one_bad_row(db_path):
    """一条失败不能拖垮整批 —— 批量最忌讳"全有或全无"的静默失败。"""
    p = db_path["path"]
    a = db_path["articles"]
    r = bulk_clean([999999, a["已生成稿"], a["已发布稿"]], db_path=p)
    assert r["done_count"] == 1
    assert r["skipped_count"] == 2
    assert _status(p, a["已生成稿"]) == "<不存在>"


# ---------------- 热点:软删 + 审计 ----------------

def test_hot_bulk_soft_delete_by_url_and_id(db_path):
    p = db_path["path"]
    d = db_path
    r = bulk_soft_delete(urls=["http://t/1"], ids=[d["topic2"]], db_path=p)
    assert r["done_count"] == 2 and r["skipped_count"] == 0

    conn = connect(p)
    try:
        rows = query(conn, "SELECT id, deleted FROM hot_topics WHERE id IN (?,?)",
                     (d["topic"], d["topic2"]))
        assert all(x["deleted"] == 1 for x in rows)
        # 合规审计必须留痕
        logs = query(conn, "SELECT COUNT(*) AS n FROM kb_clean_logs")
        assert logs[0]["n"] == 2
    finally:
        conn.close()


def test_hot_bulk_soft_delete_skips_unknown_with_reason(db_path):
    p = db_path["path"]
    r = bulk_soft_delete(urls=["http://no/such"], db_path=p)
    assert r["done_count"] == 0 and r["skipped_count"] == 1
    assert r["skipped"][0]["reason"]          # 有原因文本,不是空串


def test_hot_bulk_soft_delete_is_idempotent(db_path):
    """重复废弃同一条:第二次必须进 skipped 且带原因,而不是静默成功。"""
    p = db_path["path"]
    url = "http://t/1"
    first = bulk_soft_delete(urls=[url], db_path=p)
    second = bulk_soft_delete(urls=[url], db_path=p)
    assert first["done_count"] == 1
    assert second["done_count"] == 0 and second["skipped_count"] == 1


def test_hot_bulk_over_limit_raises(db_path):
    p = db_path["path"]
    with pytest.raises(ValueError, match=str(HOT_LIMIT)):
        bulk_soft_delete(urls=[f"http://x/{i}" for i in range(HOT_LIMIT + 1)], db_path=p)


# ---------------- 接口层 ----------------

def test_router_registers_bulk_endpoints():
    """批量接口必须真的挂在 /api 下(前端只认这两个路径)。"""
    from app.web.router import router

    paths = {r.path for r in router.routes}
    assert "/review/bulk" in paths
    assert "/hot/bulk-delete" in paths


# ---------------- 文章库计数口径 ----------------

def test_all_total_is_independent_of_filter(db_path):
    """「全部」的计数必须与当前筛选无关。

    用户报过:选中某个状态后，「全部(N)」跟着变成该状态的条数。
    原因是前端拿 `total`(= 本次筛选命中数)去显示「全部」。
    """
    from app.review.service import list_articles

    p = db_path["path"]
    every = list_articles("all", db_path=p)
    total = every["all_total"]
    assert total == len(every["rows"]) == 6            # fixture 里共 6 篇文章

    one = list_articles("generated", db_path=p)
    assert one["total"] == 1                            # 筛选命中 1 条
    assert one["all_total"] == total                    # 但「全部」不变
    # 各状态计数之和 = 全部
    assert sum(one["status_counts"].values()) == total
