"""反查发布状态的测试(全部用注入的假数据,不打网络)。

背景:文章发出去之后系统本来不知道,一直停在 `pushed` ——
于是回流(只取 published)永远筛不出待办。实测库里只有 1 篇 published,后台已有 53 条发表记录。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backfill.service import sync_published
from app.core.db import connect, init_db


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


def _add_article(conn, title: str, status: str = "pushed", appmsgid: str = "") -> int:
    url = (f"https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit"
           f"&appmsgid={appmsgid}&token=1&lang=zh_CN") if appmsgid else ""
    return conn.execute(
        """INSERT INTO articles(template_id, structure_id, title, body, status, draft_url)
           VALUES('tpl_ai_news_v1','s3',?,'正文',?,?)""",
        (title, status, url),
    ).lastrowid


def _mp_item(title: str, draft_msgid: str = "", publish_type=1, ts=1789056785,
             link="https://mp.weixin.qq.com/s/AAAA", msgid="2247484065") -> dict:
    return {"draft_msgid": draft_msgid, "msgid": msgid, "title": title, "link": link,
            "digest": "", "create_time": ts, "publish_type": publish_type, "is_deleted": False}


def _mp(items, unmass=None, massed=None) -> dict:
    unmass = len([i for i in items if str(i["publish_type"]) == "1"]) if unmass is None else unmass
    massed = len([i for i in items if str(i["publish_type"]) == "101"]) if massed is None else massed
    return {"items": items, "total_count": len(items), "publish_count": unmass,
            "masssend_count": massed, "pages": 1}


def test_exact_match_by_draft_msgid(db_path):
    conn = connect(db_path)
    aid = _add_article(conn, "巴塔哥尼亚建厂：算力成本转移的财务账", appmsgid="100000406")
    conn.commit()
    conn.close()

    r = sync_published(fetch=lambda: _mp([_mp_item("巴塔哥尼亚建厂：算力成本转移的财务账",
                                                   draft_msgid="100000406")]),
                       db_path=str(db_path))
    assert r["matched"] == 1 and r["by_draft"] == 1

    conn = connect(db_path)
    row = conn.execute("SELECT status, published_at, published_url, mp_msgid, mp_mass_sent "
                       "FROM articles WHERE id=?", (aid,)).fetchone()
    conn.close()
    assert row["status"] == "published"
    assert row["published_at"].startswith("2026-")     # 由时间戳转换而来
    assert row["published_url"] == "https://mp.weixin.qq.com/s/AAAA"
    assert row["mp_msgid"] == "2247484065"
    assert row["mp_mass_sent"] == 0                    # publish_type=1 → 未群发


def test_title_fallback_when_no_draft_msgid(db_path):
    """实测「从草稿直接群发」的发表记录**不带 draft_msgid**,只能靠标题兜底。"""
    conn = connect(db_path)
    aid = _add_article(conn, "2B模型超越GPT-5？算力账单不认跑分", appmsgid="100000401")
    conn.commit()
    conn.close()

    r = sync_published(fetch=lambda: _mp([_mp_item("2B模型超越GPT-5？算力账单不认跑分",
                                                   draft_msgid="", publish_type=101)]),
                       db_path=str(db_path))
    assert r["by_title"] == 1
    conn = connect(db_path)
    row = conn.execute("SELECT mp_mass_sent FROM articles WHERE id=?", (aid,)).fetchone()
    conn.close()
    assert row["mp_mass_sent"] == 1                    # publish_type=101 → 群发过


def test_ambiguous_title_is_not_matched(db_path):
    """同名标题在发表记录里出现多次时**不猜** —— 宁可不推进,也不能认错。"""
    conn = connect(db_path)
    aid = _add_article(conn, "重名标题", appmsgid="100000999")
    conn.commit()
    conn.close()

    r = sync_published(
        fetch=lambda: _mp([_mp_item("重名标题", link="https://x/1"),
                           _mp_item("重名标题", link="https://x/2")]),
        db_path=str(db_path))
    assert r["matched"] == 0
    conn = connect(db_path)
    assert conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()["status"] == "pushed"
    conn.close()


def test_never_pushed_article_is_not_touched(db_path):
    """**从未推送过**的稿子(generated/approved)不可能已经发表 —— 反查不碰它们。"""
    conn = connect(db_path)
    aid = _add_article(conn, "还没推过的稿子", status="generated", appmsgid="")
    conn.commit()
    conn.close()

    r = sync_published(fetch=lambda: _mp([_mp_item("还没推过的稿子", link="https://x/9")]),
                       db_path=str(db_path))
    assert r["matched"] == 0
    conn = connect(db_path)
    row = conn.execute("SELECT status, published_url FROM articles WHERE id=?", (aid,)).fetchone()
    conn.close()
    assert row["status"] == "generated" and not row["published_url"]


def test_pushed_article_without_draft_url_is_still_matched(db_path):
    """⚠️ 回归:人工「确认已发布」曾经会把 `draft_url` 清成 NULL。

    旧反查只看 `draft_url` 非空的稿子 → 这些稿子**永远查不出已发布**,
    数据回流列表也就少了它们(实测 #45 就是这样漏掉的)。
    只要推送过就该参与反查;精确匹配拿不到时用**唯一的标题**兜底。
    """
    conn = connect(db_path)
    aid = _add_article(conn, "AI办公生态：技术虚火与渠道实利", appmsgid="")
    conn.commit()
    conn.close()

    r = sync_published(
        fetch=lambda: _mp([_mp_item("AI办公生态：技术虚火与渠道实利", link="https://x/45")]),
        db_path=str(db_path))
    assert r["matched"] == 1 and r["by_title"] == 1

    conn = connect(db_path)
    row = conn.execute("SELECT status, published_url FROM articles WHERE id=?", (aid,)).fetchone()
    conn.close()
    assert row["status"] == "published" and row["published_url"] == "https://x/45"


def test_never_downgrades_and_reports_missing(db_path):
    """后台找不到的已发布文章:保留原状态并列入 missing_in_mp,便于人工核对(可能被删/改标题)。"""
    conn = connect(db_path)
    aid = _add_article(conn, "已被删除的文章", status="published", appmsgid="100000777")
    conn.execute("UPDATE articles SET published_at='2026-09-01 10:00:00' WHERE id=?", (aid,))
    conn.commit()
    conn.close()

    r = sync_published(fetch=lambda: _mp([_mp_item("毫不相干", draft_msgid="111")]),
                       db_path=str(db_path))
    assert r["matched"] == 0
    assert [m["id"] for m in r["missing_in_mp"]] == [aid]
    conn = connect(db_path)
    assert conn.execute("SELECT status FROM articles WHERE id=?", (aid,)).fetchone()["status"] == "published"
    conn.close()


def test_does_not_overwrite_existing_published_at(db_path):
    """已经有的发表时间不被后台时间覆盖(本地记录优先,避免反复同步时漂移)。"""
    conn = connect(db_path)
    aid = _add_article(conn, "已确认过的", status="published", appmsgid="100000555")
    conn.execute("UPDATE articles SET published_at='2026-09-01 08:00:00' WHERE id=?", (aid,))
    conn.commit()
    conn.close()

    sync_published(fetch=lambda: _mp([_mp_item("已确认过的", draft_msgid="100000555")]),
                   db_path=str(db_path))
    conn = connect(db_path)
    assert conn.execute("SELECT published_at FROM articles WHERE id=?",
                        (aid,)).fetchone()["published_at"] == "2026-09-01 08:00:00"
    conn.close()


def test_sync_writes_an_audit_log(db_path):
    """反查要留痕:事后能回答"这篇是怎么被认成已发布的、走的是哪种匹配"。"""
    conn = connect(db_path)
    _add_article(conn, "留痕测试", appmsgid="100000111")
    conn.commit()
    conn.close()

    sync_published(fetch=lambda: _mp([_mp_item("留痕测试", draft_msgid="100000111")]),
                   db_path=str(db_path))
    conn = connect(db_path)
    row = conn.execute("SELECT detail FROM run_logs WHERE status='publish_sync'").fetchone()
    conn.close()
    assert row and "draft_msgid" in row["detail"]


# ---------------- 纯函数:解析与自查 ----------------

def test_parse_item_flattens_nested_json():
    from app.pusher.mp_api import _parse_item

    raw = {"publish_type": 1, "publish_info": '{"type":1,"publish_info":'
           '{"draft_msgid":100000406,"msgid":2247484065,"create_time":1789056787},'
           '"appmsgex":[{"title":"标题","link":"https://x/s/1","appmsgid":2247484065}]}'}
    it = _parse_item(raw)
    assert it["draft_msgid"] == "100000406"
    assert it["title"] == "标题"
    assert it["link"] == "https://x/s/1"
    assert it["publish_type"] == 1


def test_parse_item_returns_none_on_garbage():
    from app.pusher.mp_api import _parse_item

    assert _parse_item({}) is None
    assert _parse_item({"publish_info": "不是 JSON"}) is None


def test_verify_type_mapping_detects_mismatch():
    """publish_type 的含义没有官方文档,是从计数倒推的 —— 必须能自查出不吻合。"""
    from app.pusher.mp_api import verify_type_mapping

    ok = verify_type_mapping({"items": [{"publish_type": 1}, {"publish_type": 101}],
                              "publish_count": 1, "masssend_count": 1})
    assert ok["ok"] is True
    bad = verify_type_mapping({"items": [{"publish_type": 1}, {"publish_type": 1}],
                               "publish_count": 1, "masssend_count": 1})
    assert bad["ok"] is False
