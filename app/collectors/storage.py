"""热点入库服务:抓取结果写入 hot_topics + 知识库留存(合规元数据)。"""
from __future__ import annotations

import sqlite3

from app.core.db import connect


def save_hot_topics(
    topics: list[dict],
    db_path: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> int:
    """批量入库,返回写入条数。已存在的 url(非软删)跳过。

    注意:仅接收元数据 dict(BaseHotTopic.to_dict 输出),杜绝正文入库。
    """
    own = conn is None
    c = conn or connect(db_path)
    saved = 0
    try:
        for t in topics:
            exists = c.execute(
                "SELECT id FROM hot_topics WHERE url=? AND deleted=0", (t.get("url", ""),)
            ).fetchone()
            if exists:
                continue
            c.execute(
                """INSERT INTO hot_topics
                   (source, fetch_mode, title, url, platform_hot, author, summary,
                    category, pub_at, fetch_date, expired_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t["source"], t.get("fetch_mode", "crawler"), t["title"],
                    t.get("url", ""), t.get("platform_hot", 0.0), t.get("author", ""),
                    t.get("summary", ""), t.get("category", ""),
                    t.get("pub_at", ""), t.get("fetch_date", ""), t.get("expired_at", ""),
                ),
            )
            saved += 1
        c.commit()
        return saved
    finally:
        if own:
            c.close()


def soft_delete_topic(url: str, reason: str = "manual", db_path: str | None = None) -> None:
    """一键删稿(合规要求):按链接软删除 + 记录清理日志。"""
    own = db_path is not None
    conn = connect(db_path) if db_path else connect()
    try:
        row = conn.execute(
            "SELECT id FROM hot_topics WHERE url=? AND deleted=0", (url,)
        ).fetchone()
        if row:
            conn.execute("UPDATE hot_topics SET deleted=1 WHERE id=?", (row["id"],))
            conn.execute(
                "INSERT INTO kb_clean_logs(topic_id, clean_type, reason) VALUES(?,?,?)",
                (row["id"], "manual", reason),
            )
            conn.commit()
    finally:
        conn.close()
