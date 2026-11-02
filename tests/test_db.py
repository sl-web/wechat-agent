"""数据库层测试:建库建表、增删查、幂等。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.db import connect, execute, init_db, query

TABLES = [
    "hot_topics",
    "articles",
    "article_stats",
    "templates",
    "configs",
    "schedules",
    "run_logs",
    "kb_clean_logs",
]


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


def test_init_creates_all_tables(db_path: Path):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert set(TABLES) <= names
    finally:
        conn.close()


def test_init_is_idempotent(db_path: Path):
    init_db(db_path)  # 第二次执行不应报错
    init_db(db_path)
    conn = connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table'"
        ).fetchone()["c"]
        assert count >= len(TABLES)
    finally:
        conn.close()


def test_insert_and_query_hot_topic(db_path: Path):
    conn = connect(db_path)
    tid = execute(
        conn,
        "INSERT INTO hot_topics(source, fetch_mode, title, url, platform_hot, fetch_date) VALUES(?,?,?,?,?,?)",
        ("wechat", "api", "某热点标题", "https://example.com", 100.0, "2026-09-09"),
    )
    rows = query(conn, "SELECT * FROM hot_topics WHERE id=?", (tid,))
    assert rows[0]["title"] == "某热点标题"
    assert rows[0]["source"] == "wechat"
    conn.close()


def test_unique_article_stats(db_path: Path):
    conn = connect(db_path)
    aid = execute(
        conn,
        "INSERT INTO articles(template_id, title) VALUES(?,?)",
        ("tpl_t", "测试文章"),
    )
    execute(
        conn,
        "INSERT INTO article_stats(article_id, read_count, stat_date) VALUES(?,?,?)",
        (aid, 100, "2026-09-09"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        execute(
            conn,
            "INSERT INTO article_stats(article_id, read_count, stat_date) VALUES(?,?,?)",
            (aid, 200, "2026-09-09"),
        )
    conn.close()


def test_migration_backfills_first_analyzed_at_from_fetch_date(tmp_path: Path):
    """老库补列时,first_analyzed_at 用 fetch_date 回填。

    ⚠️ 不能用 analyzed_at 回填 —— 它每次分析都被刷新成当前时间,
    拿它回填会把历史上所有条目都算成"今天首次分析",正是要修掉的那个问题。
    """
    p = tmp_path / "legacy.db"
    conn = sqlite3.connect(p)
    # 模拟"还没有 first_analyzed_at 列"的老库
    conn.execute(
        """CREATE TABLE hot_topics(
             id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, fetch_mode TEXT,
             title TEXT NOT NULL, url TEXT, platform_hot REAL, author TEXT, summary TEXT,
             category TEXT, pub_at TEXT, heat_score REAL, rank_pct REAL, analyzed_at TEXT,
             fetch_date TEXT NOT NULL, expired_at TEXT, deleted INTEGER DEFAULT 0,
             created_at TEXT DEFAULT (datetime('now','localtime')))"""
    )
    conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, analyzed_at, fetch_date, deleted)
           VALUES('rss','rss','昨天抓的','http://a','2026-09-11 10:00:00','2026-09-10',0)"""
    )
    conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, analyzed_at, fetch_date, deleted)
           VALUES('rss','rss','没分析过的','http://b',NULL,'2026-09-10',0)"""
    )
    conn.commit()
    conn.close()

    init_db(p)                      # 触发迁移

    conn = connect(p)
    rows = {
        r["title"]: r["first_analyzed_at"]
        for r in conn.execute("SELECT title, first_analyzed_at FROM hot_topics")
    }
    conn.close()
    assert rows["昨天抓的"] == "2026-09-10", "应按 fetch_date 回填,而不是被刷成今天"
    assert rows["没分析过的"] is None, "没分析过的条目不该有首次分析时间"
