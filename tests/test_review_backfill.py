"""审核/清理/回流/看板 测试(端到端的状态流转)。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backfill.service import StubStatsFetcher, backfill_once
from app.core.db import connect, init_db, query
from app.dashboard.service import dashboard_summary, review_stale
from app.generator.templates import seed_default_template
from app.knowledge.cleaner import clean_knowledge_base, soft_delete_by_url
from app.review.service import (
    approve,
    confirm_published,
    edit_article,
    get_article,
    list_review_queue,
    reject,
)


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    seed_default_template(str(p))

    conn = connect(p)
    # 1 条热点 + 2 篇文章
    tid = conn.execute(
        "INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date) VALUES('wechat','crawler','某热点','http://t/1','2026-09-09')"
    ).lastrowid
    a1 = conn.execute(
        """INSERT INTO articles(topic_id, template_id, structure_id, title, body, status)
           VALUES(?,?,?,?,?,?)""",
        (tid, "tpl_ai_news_v1", "s1", "文章一", "正文" * 100, "pushed"),
    ).lastrowid
    a2 = conn.execute(
        """INSERT INTO articles(topic_id, template_id, structure_id, title, body, status)
           VALUES(?,?,?,?,?,?)""",
        (tid, "tpl_ai_news_v1", "s1", "文章二", "正文" * 100, "generated"),
    ).lastrowid
    conn.commit()
    conn.close()
    return p, tid, a1, a2


def _path(db):
    return str(db)


# ---- 审核流转 ----
def test_review_approve(db_path):
    p, tid, a1, a2 = db_path
    r = approve(a1, db_path=_path(p))
    assert r["status"] == "approved"
    assert get_article(a1, _path(p))["status"] == "approved"


def test_review_reject_requires_reason(db_path):
    p, tid, a1, a2 = db_path
    with pytest.raises(ValueError):
        reject(a1, "", db_path=_path(p))
    r = reject(a1, "事实有误", db_path=_path(p))
    assert r["status"] == "rejected"
    assert get_article(a1, _path(p))["reject_reason"] == "事实有误"


def test_review_edit_article(db_path):
    p, tid, a1, a2 = db_path
    r = edit_article(a1, {"body": "新正文" * 50}, db_path=_path(p))
    assert r["status"] == "edited"
    art = get_article(a1, _path(p))
    assert art["body"] == "新正文" * 50


def test_review_queue_lists_pushed(db_path):
    p, tid, a1, a2 = db_path
    q = list_review_queue("pushed", db_path=_path(p))
    assert len(q) == 1 and q[0]["id"] == a1


def test_publish_then_backfill(db_path):
    p, tid, a1, a2 = db_path
    approve(a1, db_path=_path(p))
    confirm_published(a1, db_path=_path(p))
    art = get_article(a1, _path(p))
    assert art["status"] == "published"
    assert art["published_at"] is not None

    # 强制把回流日设为今天:发布日=今天,第1天就是明天 → 手动改 published_at 为昨天
    conn = connect(p)
    conn.execute(
        "UPDATE articles SET published_at=date('now','localtime','-1 day') WHERE id=?", (a1,)
    )
    conn.commit()
    conn.close()
    res = backfill_once(fetcher=StubStatsFetcher(), db_path=_path(p))
    # 发布后第 1 天应回流(今天);这里只测**排期与状态流转**,用 stub 保证离线确定性
    assert res["backfilled"] >= 1


def test_confirm_published_keeps_existing_draft_url(db_path):
    """⚠️ 回归:确认发布时传空 draft_url **不能**把已有的草稿链接清掉。

    旧实现是无条件 `draft_url=?`,而界面上这个框经常是空的 ——
    于是"确认已发布"把链接清成 NULL,反查发表记录就再也匹配不到它,
    数据回流列表也跟着少一篇(实测 #17/#45 就是这么丢的)。
    """
    p, tid, a1, a2 = db_path
    conn = connect(p)
    conn.execute(
        "UPDATE articles SET draft_url='https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=100000406' "
        "WHERE id=?", (a1,))
    conn.commit()
    conn.close()

    confirm_published(a1, "", db_path=_path(p))          # 不传 draft_url
    conn = connect(p)
    row = conn.execute("SELECT status, draft_url FROM articles WHERE id=?", (a1,)).fetchone()
    conn.close()
    assert row["status"] == "published"
    assert "appmsgid=100000406" in (row["draft_url"] or ""), "已有的草稿链接被清掉了"

    confirm_published(a1, "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=999",
                      db_path=_path(p))                   # 传了非空 → 应更新
    conn = connect(p)
    row = conn.execute("SELECT draft_url FROM articles WHERE id=?", (a1,)).fetchone()
    conn.close()
    assert "appmsgid=999" in row["draft_url"]


# ---- 知识库清理 ----
def test_clean_removes_expired_and_dup(db_path):
    p, tid, a1, a2 = db_path
    conn = connect(p)
    # 造一条过期热点 + 一条与 tid 同标题的热点
    conn.execute(
        "INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date, expired_at) VALUES('wechat','crawler','过期','http://old', '2026-09-09','2020-01-01')"
    )
    conn.execute(
        "INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date) VALUES('wechat','crawler','某热点','http://dup','2026-09-09')"
    )
    conn.commit()
    conn.close()
    stats = clean_knowledge_base(db_path=_path(p))
    assert stats["retention"] >= 1   # 过期清理
    assert stats["dup"] >= 1         # 标题重复清理


def test_soft_delete_by_url(db_path):
    p, tid, a1, a2 = db_path
    ok = soft_delete_by_url("http://t/1", db_path=_path(p))
    assert ok is True
    conn = connect(p)
    row = conn.execute("SELECT deleted FROM hot_topics WHERE id=?", (tid,)).fetchone()
    log = query(conn, "SELECT clean_type FROM kb_clean_logs WHERE topic_id=?", (tid,))
    conn.close()
    assert row["deleted"] == 1
    assert log[0]["clean_type"] == "manual"


# ---- 看板 ----
def test_dashboard_summary(db_path):
    p, tid, a1, a2 = db_path
    s = dashboard_summary(db_path=_path(p))
    assert s["generated_today"] >= 0
    assert "top_articles" in s and "pending_review" in s


def test_review_stale_detection(db_path):
    p, tid, a1, a2 = db_path
    # 把文章一改得很久以前 → 应被识别为滞留
    conn = connect(p)
    conn.execute(
        "UPDATE articles SET created_at=datetime('now','localtime','-3 day') WHERE id=?", (a1,)
    )
    conn.commit()
    conn.close()
    stale = review_stale(db_path=_path(p), stale_hours=24)
    ids = [s["id"] for s in stale]
    assert a1 in ids
