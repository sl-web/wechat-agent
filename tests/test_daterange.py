"""时间范围筛选的测试(分析报告 / 文章库,默认当天)。

口径必须统一:看板、分析报告、文章库说的"今天"要是同一个今天,
否则用户对不上账(这正是这次要解决的问题)。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.core.daterange import normalize_range, range_bounds, range_label
from app.core.db import connect, init_db

TODAY = date.today()
TODAY_S = TODAY.isoformat()
D3 = (TODAY - timedelta(days=2)).isoformat()
D10 = (TODAY - timedelta(days=9)).isoformat()
D40 = (TODAY - timedelta(days=39)).isoformat()


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


# ---------------- 范围解析 ----------------

def test_normalize_range_whitelist():
    assert normalize_range("today") == "today"
    assert normalize_range("7d") == "7d"
    assert normalize_range("ALL") == "all"
    # 非法值一律按默认(当天),不能让前端传个乱七八糟的值就把过滤关掉
    assert normalize_range("") == "today"
    assert normalize_range(None) == "today"
    assert normalize_range("90d") == "today"
    assert normalize_range("; DROP TABLE") == "today"


def test_range_bounds_include_today():
    """近 N 天必须**含今天**,而不是"从 N 天前到昨天"。"""
    assert range_bounds("today", TODAY) == (TODAY_S, TODAY_S)
    assert range_bounds("7d", TODAY) == ((TODAY - timedelta(days=6)).isoformat(), TODAY_S)
    assert range_bounds("30d", TODAY) == ((TODAY - timedelta(days=29)).isoformat(), TODAY_S)
    assert range_bounds("all", TODAY) == (None, None)


def test_range_labels():
    assert range_label("today") == "今天"
    assert range_label("all") == "全部时间"
    assert range_label("bogus") == "今天"


# ---------------- 分析报告按范围实时聚类 ----------------

def _add_topic(conn, title: str, fetch_date: str, heat: float = 0.5) -> int:
    return conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, summary, fetch_date,
                                  heat_score, deleted)
           VALUES('rss','rss',?,?,?,?,?,0)""",
        (title, f"http://x/{title}", f"AI 算力 芯片 成本 {title}", fetch_date, heat),
    ).lastrowid


def test_report_for_range_filters_by_fetch_date(db_path):
    from app.analyzer.service import report_for_range

    conn = connect(db_path)
    _add_topic(conn, "今天的", TODAY_S)
    _add_topic(conn, "三天前的", D3)
    _add_topic(conn, "四十天前的", D40)
    conn.commit()
    conn.close()

    p = str(db_path)
    today_out = report_for_range("today", db_path=p, today=TODAY_S)
    assert today_out["source_count"] == 1
    assert today_out["range_label"] == "今天"
    assert today_out["live"] is True

    week = report_for_range("7d", db_path=p, today=TODAY_S)
    assert week["source_count"] == 2          # 今天 + 三天前

    all_out = report_for_range("all", db_path=p, today=TODAY_S)
    assert all_out["source_count"] == 3       # 全部时间不过滤


def test_report_for_range_empty_is_not_an_error(db_path):
    """该范围没有素材时,要给出可执行的说明,而不是 500。"""
    from app.analyzer.service import report_for_range

    out = report_for_range("today", db_path=str(db_path), today=TODAY_S)
    assert out["report"] is None
    assert out["source_count"] == 0
    assert "没有热点素材" in out["empty_reason"]


def test_report_for_range_excludes_deleted(db_path):
    from app.analyzer.service import report_for_range

    conn = connect(db_path)
    keep = _add_topic(conn, "保留", TODAY_S)
    gone = _add_topic(conn, "废弃", TODAY_S)
    conn.execute("UPDATE hot_topics SET deleted=1 WHERE id=?", (gone,))
    conn.commit()
    conn.close()

    out = report_for_range("today", db_path=str(db_path), today=TODAY_S)
    assert out["source_count"] == 1
    assert keep


def test_report_range_does_not_write_anything(db_path):
    """报告是**只读**的:不能因为看一眼报告就把分析时间刷了。"""
    from app.analyzer.service import report_for_range

    conn = connect(db_path)
    tid = _add_topic(conn, "今天", TODAY_S)
    conn.commit()
    conn.close()

    report_for_range("all", db_path=str(db_path), today=TODAY_S)

    conn = connect(db_path)
    row = conn.execute(
        "SELECT analyzed_at, first_analyzed_at FROM hot_topics WHERE id=?", (tid,)
    ).fetchone()
    conn.close()
    assert row["analyzed_at"] is None, "看报告不应写入 analyzed_at"
    assert row["first_analyzed_at"] is None


# ---------------- 文章库按范围筛选 ----------------

def _add_article(conn, title: str, status: str, created: str) -> int:
    return conn.execute(
        """INSERT INTO articles(template_id, structure_id, title, body, status, created_at)
           VALUES('tpl_ai_news_v1','s3',?,'正文',?,?)""",
        (title, status, created),
    ).lastrowid


def test_list_articles_filters_by_range(db_path):
    from app.review.service import list_articles

    conn = connect(db_path)
    _add_article(conn, "今天生成的", "generated", f"{TODAY_S} 10:00:00")
    _add_article(conn, "三天前生成的", "generated", f"{D3} 10:00:00")
    conn.commit()
    conn.close()

    p = str(db_path)
    today_out = list_articles("all", db_path=p, range_key="today")
    assert today_out["total"] == 1
    assert today_out["all_total"] == 1
    assert today_out["range"] == "today"

    week = list_articles("all", db_path=p, range_key="7d")
    assert week["total"] == 2

    everything = list_articles("all", db_path=p, range_key="all")
    assert everything["total"] == 2
    assert everything["all_total"] == 2


def test_article_counts_share_the_same_time_scope(db_path):
    """状态计数必须与列表**同一时间口径**,否则会出现
    "选了今天、「已生成」还显示历史总数"的对不上。"""
    from app.review.service import list_articles

    conn = connect(db_path)
    _add_article(conn, "今天生成", "generated", f"{TODAY_S} 09:00:00")
    _add_article(conn, "三天前生成", "generated", f"{D3} 09:00:00")
    _add_article(conn, "三天前推送", "pushed", f"{D3} 11:00:00")
    conn.commit()
    conn.close()

    today_out = list_articles("all", db_path=str(db_path), range_key="today")
    assert today_out["status_counts"] == {"generated": 1}
    assert today_out["all_total"] == sum(today_out["status_counts"].values())

    # 选单个状态时,「全部」不变 —— 这是上一条修过的口径,不能被范围筛选破坏
    one = list_articles("generated", db_path=str(db_path), range_key="7d")
    assert one["total"] == 2
    assert one["all_total"] == 3


def test_list_articles_without_range_is_unfiltered(db_path):
    """不传 range 时不加时间条件(向后兼容既有调用方)。"""
    from app.review.service import list_articles

    conn = connect(db_path)
    _add_article(conn, "很老的", "generated", "2020-01-01 09:00:00")
    conn.commit()
    conn.close()
    assert list_articles("all", db_path=str(db_path))["total"] == 1
