"""回流反馈数据(指标抓取 + 落库 + 看板)的测试。全部用注入的假数据,不打网络。

背景:公众号「发表记录」的 HTML 变体内联了每篇的指标,而 `article_stats`
原先只有 read/like/share/favorite 四列、默认抓取器是固定返回 0 的占位实现 ——
所以"回流"以前是假的。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.backfill.service import (
    STAT_COLUMNS,
    MpStatsFetcher,
    StubStatsFetcher,
    backfill_all,
    backfill_article,
    stats_dashboard,
)
from app.core.db import connect, init_db


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


def _add(conn, title: str, appmsgid: str = "100000406", status: str = "published",
         published_at: str = "2026-09-10 08:00:00") -> int:
    url = (f"https://mp.weixin.qq.com/cgi-bin/appmsg?action=edit&appmsgid={appmsgid}"
           "&token=1&lang=zh_CN") if appmsgid else ""
    return conn.execute(
        """INSERT INTO articles(template_id, structure_id, title, body, status,
                                draft_url, published_at)
           VALUES('tpl_ai_news_v1','s3',?,'正文',?,?,?)""",
        (title, status, url, published_at),
    ).lastrowid


def _mp_item(title: str, draft_msgid: str = "", stats: dict | None = None) -> dict:
    return {"draft_msgid": draft_msgid, "msgid": "2247484065", "title": title,
            "link": "https://mp.weixin.qq.com/s/AAAA", "digest": "",
            "create_time": 1789056785, "publish_type": 1, "is_deleted": False,
            "stats": stats or {}}


def _mp(items) -> dict:
    return {"items": items, "total_count": len(items), "publish_count": len(items),
            "masssend_count": 0, "pages": 1}


FULL_STATS = {"read_count": 23, "like_count": 2, "share_count": 1,
              "recommend_count": 3, "comment_count": 0}


# ---------------- 指标字段映射 ----------------

def test_stat_map_matches_backend_dom_semantics():
    """⚠️ 后台的「点赞」存在 old_like_num、「推荐」(原在看)存在 like_num —— 反直觉。

    这条测试**钉死**这个映射:单看字段名很容易"顺手改回来",那样标签就和后台对不上了。
    """
    from app.pusher.mp_api import STAT_MAP

    assert STAT_MAP["read_count"] == "read_num"
    assert STAT_MAP["like_count"] == "old_like_num"      # 不是 like_num
    assert STAT_MAP["share_count"] == "share_num"
    assert STAT_MAP["recommend_count"] == "like_num"     # 不是 old_like_num
    assert STAT_MAP["comment_count"] == "comment_num"


def test_parse_item_reads_stats_from_html_variant():
    """HTML 变体用 `appmsg_info` 且链接字段叫 `content_url`(JSON 变体是 `appmsgex`/`link`)。"""
    from app.pusher.mp_api import _parse_item

    raw = {"publish_type": 101, "publish_info": (
        '{"type":10002,"publish_info":{"draft_msgid":100000406,"msgid":2247484065,'
        '"create_time":1789056785},"appmsg_info":[{"appmsgid":2247484065,"itemidx":1,'
        '"title":"标题","content_url":"https://mp.weixin.qq.com/s/XX","read_num":23,'
        '"old_like_num":2,"share_num":1,"like_num":3,"comment_num":4}]}')}
    it = _parse_item(raw)
    assert it["draft_msgid"] == "100000406"
    assert it["link"] == "https://mp.weixin.qq.com/s/XX"
    assert it["stats"] == {"read_count": 23, "like_count": 2, "share_count": 1,
                           "recommend_count": 3, "comment_count": 4}


def test_parse_item_without_stats_gives_empty_dict():
    """取不到指标时**不放键** —— 调用方据此区分"没有数据"和"数据是 0"。"""
    from app.pusher.mp_api import _parse_item

    raw = {"publish_type": 1, "publish_info": (
        '{"publish_info":{"draft_msgid":1},"appmsgex":[{"title":"t","link":"u"}]}')}
    it = _parse_item(raw)
    assert it["stats"] == {}
    assert it["title"] == "t" and it["link"] == "u"


# ---------------- 抓取器 ----------------

def test_fetcher_matches_by_draft_msgid():
    f = MpStatsFetcher(loader=lambda: _mp([_mp_item("无关", "999", {"read_count": 5}),
                                           _mp_item("目标", "100000406", FULL_STATS)]))
    art = {"title": "标题被改过也无所谓",
           "draft_url": "https://mp.weixin.qq.com/cgi-bin/appmsg?appmsgid=100000406&token=1"}
    assert f.fetch(art, "2026-09-11") == FULL_STATS


def test_fetcher_title_fallback_only_when_unique():
    art = {"title": "唯一标题", "draft_url": "https://x/appmsgid=0"}
    ok = MpStatsFetcher(loader=lambda: _mp([_mp_item("唯一标题", "", {"read_count": 7})]))
    assert ok.fetch(art, "2026-09-11") == {"read_count": 7}

    amb = MpStatsFetcher(loader=lambda: _mp([_mp_item("唯一标题", "", {"read_count": 7}),
                                             _mp_item("唯一标题", "", {"read_count": 9})]))
    assert amb.fetch(art, "2026-09-11") == {}      # 重名不猜


def test_fetcher_returns_empty_when_not_found():
    f = MpStatsFetcher(loader=lambda: _mp([_mp_item("别的文章", "1", {"read_count": 1})]))
    assert f.fetch({"title": "找不到我", "draft_url": "appmsgid=42"}, "2026-09-11") == {}


def test_fetcher_loads_publish_page_only_once():
    """一次回流要处理多篇 —— 不能每篇都去请求后台(既慢又把人家的后台当压测目标)。"""
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return _mp([_mp_item("甲", "1", {"read_count": 1}),
                    _mp_item("乙", "2", {"read_count": 2})])

    f = MpStatsFetcher(loader=loader)
    f.fetch({"title": "甲", "draft_url": "appmsgid=1"}, "d")
    f.fetch({"title": "乙", "draft_url": "appmsgid=2"}, "d")
    assert calls["n"] == 1


# ---------------- 落库 ----------------

def test_backfill_all_writes_all_metrics(db_path):
    conn = connect(db_path)
    aid = _add(conn, "目标文章", appmsgid="100000406")
    conn.commit()
    conn.close()

    r = backfill_all(fetcher=MpStatsFetcher(
        loader=lambda: _mp([_mp_item("目标文章", "100000406", FULL_STATS)])),
        db_path=str(db_path))
    assert r["backfilled"] == 1

    conn = connect(db_path)
    row = conn.execute("SELECT * FROM article_stats WHERE article_id=?", (aid,)).fetchone()
    conn.close()
    for col in STAT_COLUMNS:
        assert row[col] == FULL_STATS.get(col), col


def test_same_day_refresh_overwrites_not_ignores(db_path):
    """手动"刷新"必须能看到变化 —— 老实现用 INSERT OR IGNORE,同一天再点等于无效。"""
    conn = connect(db_path)
    aid = _add(conn, "文章", appmsgid="100000406")
    conn.commit()
    conn.close()

    for read in (10, 42):
        backfill_all(fetcher=MpStatsFetcher(
            loader=lambda r=read: _mp([_mp_item("文章", "100000406", {"read_count": r})])),
            db_path=str(db_path))

    conn = connect(db_path)
    rows = conn.execute("SELECT read_count FROM article_stats WHERE article_id=?", (aid,)).fetchall()
    conn.close()
    assert len(rows) == 1                    # 一天一行,不重复
    assert rows[0]["read_count"] == 42        # 且是新值


def test_backfill_skips_when_no_data_instead_of_writing_zeros(db_path):
    """后台找不到这篇时**跳过**,绝不写 0 —— 0 是"确实没人读",不能拿来冒充"不知道"。"""
    conn = connect(db_path)
    aid = _add(conn, "未发表的文章", appmsgid="100000999")
    conn.commit()
    conn.close()

    r = backfill_all(fetcher=MpStatsFetcher(loader=lambda: _mp([])), db_path=str(db_path))
    assert r["backfilled"] == 0 and len(r["skipped"]) == 1

    conn = connect(db_path)
    n = conn.execute("SELECT COUNT(*) c FROM article_stats WHERE article_id=?", (aid,)).fetchone()["c"]
    conn.close()
    assert n == 0


def test_backfill_article_reports_not_found(db_path):
    conn = connect(db_path)
    _add(conn, "找不到", appmsgid="100000888")
    conn.commit()
    conn.close()

    r = backfill_article(1, fetcher=MpStatsFetcher(loader=lambda: _mp([])), db_path=str(db_path))
    assert r["ok"] is False and "没找到" in r["error"]


def test_session_error_aborts_with_actionable_hint(db_path):
    """会话失效是整体性问题:不该逐篇报错刷屏,而要中止并给可执行提示。"""
    from app.pusher.mp_api import MpSessionError

    conn = connect(db_path)
    _add(conn, "甲", appmsgid="1")
    _add(conn, "乙", appmsgid="2")
    conn.commit()
    conn.close()

    class Boom:
        def fetch(self, article, stat_date):
            raise MpSessionError("会话已过期")

    r = backfill_all(fetcher=Boom(), db_path=str(db_path))
    assert "会话" in r["error"] and "重新扫码" in r["hint"]
    assert r["failed"] == []                 # 不是"失败",是"中止"


def test_stub_fetcher_still_writes_zeros(db_path):
    """测试/演示用的 stub 语义不变(全 0 = 有数据但都是 0),与"取不到"区分开。"""
    conn = connect(db_path)
    aid = _add(conn, "文章", appmsgid="100000406")
    conn.commit()
    conn.close()

    backfill_all(fetcher=StubStatsFetcher(), db_path=str(db_path))
    conn = connect(db_path)
    row = conn.execute("SELECT read_count FROM article_stats WHERE article_id=?", (aid,)).fetchone()
    conn.close()
    assert row is not None and row["read_count"] == 0


# ---------------- 看板 ----------------

def test_dashboard_lists_published_without_draft_url(db_path):
    """⚠️ 核心回归:**看板与写入侧必须同一个口径**。

    曾经看板额外要求 `draft_url` 非空,而排期回流不看它 —— 结果库里有回流数据、
    看板却不显示(实测:库 7 行、看板只列 5 篇)。`draft_url` 只是**匹配手段**,
    不是"是不是我们的文章"的判据;`articles` 表里本来就只有本系统产出的稿子。
    """
    conn = connect(db_path)
    _add(conn, "有草稿链接的", appmsgid="100000406")
    _add(conn, "链接被清掉的", appmsgid="")
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    assert {r["title"] for r in d["rows"]} == {"链接被清掉的", "有草稿链接的"}


def test_dashboard_scope_equals_backfill_scope(db_path):
    """看板列出的集合 == 回流写入侧选中的集合(`_published_articles` 是唯一真源)。"""
    from app.backfill.service import _published_articles

    conn = connect(db_path)
    _add(conn, "甲", appmsgid="1")
    _add(conn, "乙", appmsgid="")
    _add(conn, "还没发布", appmsgid="2", status="pushed", published_at=None)
    conn.commit()
    write_ids = {a["id"] for a in _published_articles(conn)}
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    assert {r["id"] for r in d["rows"]} == write_ids
    assert len(write_ids) == 2


def test_dashboard_flags_articles_not_found_in_mp(db_path):
    """后台找不到的文章要标 `in_mp=False`(可能未发表/被删/改了标题),供人工核对。"""
    conn = connect(db_path)
    _add(conn, "后台没有的", appmsgid="1")
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    assert d["rows"][0]["in_mp"] is False
    assert d["summary"]["not_in_mp"] == 1


def test_dashboard_hides_legacy_stub_rows(db_path):
    """旧占位抓取器写的**假 0** 不能显示 —— 否则会被当成"确实没人读"。

    特征:新列(recommend_count/comment_count)全空(那时还没这两列)+ 老列有值。
    真实抓取一定会写 recommend_count,所以这个判据是可靠的。
    """
    from app.backfill.service import _is_stub_row

    conn = connect(db_path)
    aid = _add(conn, "有假数据的", appmsgid="1")
    conn.commit()
    conn.execute(
        """INSERT INTO article_stats(article_id, read_count, like_count, share_count, stat_date)
           VALUES(?, 0, 0, 0, '2026-09-09')""", (aid,))
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    row = d["rows"][0]
    assert _is_stub_row({"recommend_count": None, "comment_count": None}) is True
    assert row["latest"] is None and row["history"] == []      # 假数据不展示
    assert d["summary"]["with_stats"] == 0
    assert d["rows"][0]["in_mp"] is False                       # 但这一行仍然列出来


def test_dashboard_keeps_real_rows(db_path):
    """真实抓取的行照常显示并计入汇总(有 recommend_count 就是真数据)。"""
    conn = connect(db_path)
    aid = _add(conn, "有真数据的", appmsgid="1")
    conn.commit()
    conn.execute(
        """INSERT INTO article_stats(article_id, read_count, like_count, recommend_count,
                                      comment_count, stat_date)
           VALUES(?, 23, 2, 0, 0, '2026-09-10')""", (aid,))
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    assert d["summary"]["with_stats"] == 1
    assert d["summary"]["totals"]["read_count"] == 23
    assert d["rows"][0]["latest"]["read_count"] == 23


def test_dashboard_totals_and_history(db_path):
    conn = connect(db_path)
    aid = _add(conn, "甲", appmsgid="1")
    conn.commit()
    conn.execute(
        """INSERT INTO article_stats(article_id, read_count, like_count, recommend_count,
                                      comment_count, stat_date)
           VALUES(?, 10, 1, 0, 0, '2026-09-09')""", (aid,))
    conn.execute(
        """INSERT INTO article_stats(article_id, read_count, like_count, recommend_count,
                                      comment_count, stat_date)
           VALUES(?, 23, 2, 0, 0, '2026-09-10')""", (aid,))
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    assert d["summary"]["totals"]["read_count"] == 23      # 取**最新**那天,不是累加
    row = d["rows"][0]
    assert row["stat_date"] == "2026-09-10"
    assert [h["read_count"] for h in row["history"]] == [10, 23]
    assert row["latest"]["read_count"] == 23


def test_dashboard_range_filters_by_published_at(db_path):
    conn = connect(db_path)
    _add(conn, "老文章", appmsgid="1", published_at="2020-01-01 10:00:00")
    _add(conn, "新文章", appmsgid="2", published_at="2026-09-10 10:00:00")
    conn.commit()
    conn.close()

    recent = stats_dashboard("7d", db_path=str(db_path), )
    assert [r["title"] for r in recent["rows"]] == ["新文章"]
    assert len(stats_dashboard("all", db_path=str(db_path))["rows"]) == 2


# ---------------- 迁移 ----------------

def test_migration_adds_new_stat_columns(tmp_path):
    """老库的 article_stats 只有四列,启动时要幂等补齐。"""
    import sqlite3

    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE article_stats(
             id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER NOT NULL,
             read_count INTEGER, like_count INTEGER, share_count INTEGER,
             favorite_count INTEGER, stat_date TEXT NOT NULL,
             fetched_at TEXT, UNIQUE(article_id, stat_date))""")
    conn.commit()
    conn.close()

    init_db(p)

    conn = connect(p)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(article_stats)")]
    conn.close()
    for col in ("recommend_count", "comment_count", "underline_count"):
        assert col in cols


# ---------------- 一键刷新:反查与抓取必须同源 ----------------

def test_refresh_all_syncs_before_fetching(db_path):
    """回归 #45/#46:没有草稿链接、只能按标题匹配的稿子,

    一次「立即抓取最新数据」之后应该**既有公开链接(反查写的)、又有指标(抓取写的)**,
    不该出现"有反馈数据、却标着后台未找到"的矛盾行。
    """
    from app.backfill.service import refresh_all

    conn = connect(db_path)
    # 没有 appmsgid → 只能靠标题兜底匹配(正是 #45/#46 的情形)
    _add(conn, "AI办公生态：技术虚火与渠道实利", appmsgid="")
    conn.commit()
    conn.close()

    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return _mp([_mp_item("AI办公生态：技术虚火与渠道实利", stats=FULL_STATS)])

    out = refresh_all(db_path=str(db_path), loader=loader)
    assert out["sync"]["matched"] == 1
    assert out["stats"]["backfilled"] == 1
    # 反查与抓取共用同一份后台快照 —— 不能各请求一次(别把人家的后台当压测目标)
    assert calls["n"] == 1

    d = stats_dashboard("all", db_path=str(db_path))
    row = d["rows"][0]
    assert row["in_mp"] is True                     # 链接已回填 → 不再"未找到"
    assert row["latest"]["read_count"] == 23        # 指标也拿到了
    assert d["summary"]["not_in_mp"] == 0
    assert d["summary"]["pending_check"] == 0


def test_refresh_all_only_due_keeps_schedule(db_path):
    """`only_due=True`(定时回流)也走反查,但抓取仍只处理当天到期的(T+1)。"""
    from datetime import datetime, timedelta

    from app.backfill.service import refresh_all

    conn = connect(db_path)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # T+1:昨天发的 → 今天到期
    _add(conn, "昨天发的", appmsgid="100000406", published_at=f"{yesterday} 08:00:00")
    _add(conn, "很久以前发的", appmsgid="100000407", published_at="2020-01-01 08:00:00")
    conn.commit()
    conn.close()

    out = refresh_all(db_path=str(db_path), only_due=True, loader=lambda: _mp([
        _mp_item("昨天发的", "100000406", FULL_STATS),
        _mp_item("很久以前发的", "100000407", FULL_STATS),
    ]))
    assert out["sync"]["matched"] == 2              # 反查不看排期:两篇都对上后台
    assert out["stats"]["checked"] == 1             # 抓取仍按 T+1/3/7 排期


def test_dashboard_flags_pending_check_when_stats_but_no_link(db_path):
    """有指标但没回填链接 = 「待核对」,不能和"真的后台找不到"混为一谈。"""
    conn = connect(db_path)
    aid = _add(conn, "只能按标题匹配的", appmsgid="")
    conn.commit()
    conn.execute(
        """INSERT INTO article_stats(article_id, read_count, recommend_count,
                                     comment_count, stat_date)
           VALUES(?, 5, 0, 0, '2026-09-11')""", (aid,))
    conn.commit()
    conn.close()

    d = stats_dashboard("all", db_path=str(db_path))
    s = d["summary"]
    assert s["with_stats"] == 1
    assert s["not_in_mp"] == 1
    assert s["pending_check"] == 1        # 前端据此显示「待核对」而不是「未找到」
    assert d["rows"][0]["in_mp"] is False
    assert d["rows"][0]["latest"]["read_count"] == 5
