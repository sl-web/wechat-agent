"""看板指标口径测试。

重点是「今日分析」:它一度被算成"库里热点总数",因为按 `analyzed_at` 统计 ——
而 `analyzed_at` 每次跑分析都会被刷新成当前时间,于是历史上所有条目都被算进今天。
现在按 `first_analyzed_at`(首次被分析,只写一次)统计。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from app.analyzer.service import persist_scores
from app.core.db import connect, init_db

TODAY = date.today().strftime("%Y-%m-%d")
YESTERDAY = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


def _add_topic(conn, title: str, fetch_date: str) -> int:
    return conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date)
           VALUES('rss','rss',?,?,?)""",
        (title, f"http://x/{title}", fetch_date),
    ).lastrowid


def test_first_analyzed_at_is_written_once(db_path):
    """analyzed_at 每次都刷新,first_analyzed_at 只写一次。"""
    conn = connect(db_path)
    tid = _add_topic(conn, "甲", TODAY)
    conn.commit()
    conn.close()

    persist_scores([{"id": tid, "heat_score": 0.5, "rank_pct": 0.5}], db_path=str(db_path))
    conn = connect(db_path)
    first = conn.execute(
        "SELECT analyzed_at, first_analyzed_at FROM hot_topics WHERE id=?", (tid,)
    ).fetchone()
    conn.close()

    persist_scores([{"id": tid, "heat_score": 0.9, "rank_pct": 0.9}], db_path=str(db_path))
    conn = connect(db_path)
    second = conn.execute(
        "SELECT heat_score, analyzed_at, first_analyzed_at FROM hot_topics WHERE id=?", (tid,)
    ).fetchone()
    conn.close()

    assert first["first_analyzed_at"] == second["first_analyzed_at"], "首次分析时间被覆盖了"
    assert second["heat_score"] == 0.9, "热度应当被刷新"


def test_analyzed_today_counts_first_analysis_not_reanalysis(db_path):
    """重跑分析不能把昨天/前天的条目算进「今日分析」。"""
    from app.dashboard.service import dashboard_summary

    conn = connect(db_path)
    fresh = _add_topic(conn, "今天抓的", TODAY)
    old = _add_topic(conn, "昨天抓的", YESTERDAY)
    conn.commit()
    conn.close()

    # 两批都被"今天这次分析"刷过 analyzed_at
    persist_scores([
        {"id": fresh, "heat_score": 1.0, "rank_pct": 1.0},
        {"id": old, "heat_score": 0.8, "rank_pct": 0.8},
    ], db_path=str(db_path))
    conn = connect(db_path)
    conn.execute("UPDATE hot_topics SET first_analyzed_at=? WHERE id=?", (YESTERDAY, old))
    conn.commit()
    conn.close()

    s = dashboard_summary(db_path=str(db_path), today=TODAY)
    assert s["analyzed_today"] == 1, "只应统计今天首次被分析的条目"
    assert s["fetch_count"] == 1
    # 两条的 analyzed_at 都是今天 —— 这正是不能用它统计的原因
    conn = connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM hot_topics WHERE date(analyzed_at)=?", (TODAY,)
    ).fetchone()[0]
    conn.close()
    assert n == 2


def test_analyzed_today_excludes_deleted(db_path):
    """删除(软删)的条目不该再计入指标 —— 与今日抓取口径一致。"""
    from app.dashboard.service import dashboard_summary

    conn = connect(db_path)
    keep = _add_topic(conn, "保留", TODAY)
    gone = _add_topic(conn, "废弃", TODAY)
    conn.commit()
    conn.close()
    persist_scores([
        {"id": keep, "heat_score": 1.0, "rank_pct": 1.0},
        {"id": gone, "heat_score": 1.0, "rank_pct": 1.0},
    ], db_path=str(db_path))
    conn = connect(db_path)
    conn.execute("UPDATE hot_topics SET deleted=1 WHERE id=?", (gone,))
    conn.commit()
    conn.close()

    assert dashboard_summary(db_path=str(db_path), today=TODAY)["analyzed_today"] == 1


def test_metric_detail_uses_same_scope_as_count(db_path):
    """明细与计数必须同源 —— 否则会出现"显示 1 条、点开却是 2 条"。"""
    from app.dashboard.service import metric_detail

    conn = connect(db_path)
    today_id = _add_topic(conn, "今天的", TODAY)
    old_id = _add_topic(conn, "昨天的", YESTERDAY)
    conn.commit()
    conn.close()
    persist_scores([
        {"id": today_id, "heat_score": 1.0, "rank_pct": 1.0},
        {"id": old_id, "heat_score": 1.0, "rank_pct": 1.0},
    ], db_path=str(db_path))
    conn = connect(db_path)
    conn.execute("UPDATE hot_topics SET first_analyzed_at=? WHERE id=?", (YESTERDAY, old_id))
    conn.commit()
    conn.close()

    d = metric_detail("analyzed_today", today=TODAY, db_path=str(db_path))
    ids = [r["id"] for r in d["rows"]]
    assert ids == [today_id]
