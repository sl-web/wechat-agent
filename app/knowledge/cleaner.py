"""知识库清理服务(PRD 5.7 + 5.1.5 合规):按策略清理并记录审计日志。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from app.core.config import get_config
from app.core.db import connect


def clean_knowledge_base(conn: sqlite3.Connection | None = None, db_path: str | None = None) -> dict:
    """执行清理策略,返回各类型清理数量。

    规则:0 合规期限(expired_at 过期)/ 1 主题重复(相似标题)/ 2 过时(同日期,按保留期限)
         / 3 低价值(热度极低且无摘要)
    """
    own = conn is None
    c = conn or connect(db_path)
    stats = {"retention": 0, "dup": 0, "low_value": 0, "outdated": 0}

    retention_days = int(get_config("retention_days", c) or 30)
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    # 0. 合规期限清理
    # 0. 合规期限清理(expired_at 非空且过期;或未设置时按 fetch_date 回退判断)
    today_str = datetime.now().strftime("%Y-%m-%d")
    rows = c.execute(
        """SELECT id, title, url, expired_at FROM hot_topics WHERE deleted=0 AND (
             (expired_at IS NOT NULL AND expired_at < ?)
             OR (expired_at IS NULL AND fetch_date < ?)
           )""",
        (today_str, cutoff),
    ).fetchall()
    for r in rows:
        _delete_and_log(c, r["id"], "retention",
                        f"超过合规存储期限(截止 {r['expired_at']})")
        stats["retention"] += 1

    # 1. 主题重复:同 fetch_date 内标题相似度 ≥ 阈值,保留热度最高
    dup_rows = c.execute(
        """SELECT id, title, platform_hot, fetch_date FROM hot_topics
           WHERE deleted=0 AND fetch_date >= ? ORDER BY platform_hot DESC""",
        (cutoff,),
    ).fetchall()
    seen_titles: dict[str, int] = {}
    for r in dup_rows:
        norm = _normalize_title(r["title"])
        if not norm:
            continue
        if norm in seen_titles:
            _delete_and_log(c, r["id"], "dup", f"与热点 #{seen_titles[norm]} 标题重复")
            stats["dup"] += 1
        else:
            seen_titles[norm] = r["id"]

    # 3. 低价值:无链接无摘要且热度极低
    low_rows = c.execute(
        """SELECT id, title, url, summary, platform_hot FROM hot_topics
           WHERE deleted=0 AND (url IS NULL OR url='') AND (summary IS NULL OR summary='')
             AND (platform_hot IS NULL OR platform_hot < 1)"""
    ).fetchall()
    for r in low_rows:
        _delete_and_log(c, r["id"], "low_value", "低价值:无链接无摘要且热度极低")
        stats["low_value"] += 1

    c.commit()
    if own:
        c.close()
    return stats


def _normalize_title(title: str) -> str:
    """标题规整:去空白/标点/emoji 后小写。"""
    import re

    t = re.sub(r"[\s\W_]+", "", title or "").lower()
    return t


def _delete_and_log(conn: sqlite3.Connection, topic_id: int, clean_type: str, reason: str) -> None:
    conn.execute("UPDATE hot_topics SET deleted=1 WHERE id=?", (topic_id,))
    conn.execute(
        "INSERT INTO kb_clean_logs(topic_id, clean_type, reason) VALUES(?,?,?)",
        (topic_id, clean_type, reason),
    )


def soft_delete_by_url(url: str, reason: str = "manual", conn: sqlite3.Connection | None = None,
                       db_path: str | None = None) -> bool:
    """一键删稿(合规):按链接软删除。"""
    own = conn is None
    c = conn or connect(db_path)
    try:
        row = c.execute(
            "SELECT id FROM hot_topics WHERE url=? AND deleted=0", (url,)
        ).fetchone()
        if row:
            _delete_and_log(c, row["id"], "manual", reason)
            c.commit()
            return True
        return False
    finally:
        if own:
            c.close()


# 单次批量上限(与文章批量保持一致的量级)
BULK_LIMIT = 200


def bulk_soft_delete(urls=None, ids=None, reason: str = "manual_bulk",
                     conn: sqlite3.Connection | None = None,
                     db_path: str | None = None) -> dict:
    """批量软删除热点(分析报告里「废弃」素材)。

    走与单条相同的 `_delete_and_log` → `hot_topics.deleted=1` + `kb_clean_logs` 审计,
    不写第二套 SQL,否则合规日志会出现两套口径。

    返回 {done:[{id,url,title}], skipped:[{key, reason}], done_count, skipped_count}
    """
    own = conn is None
    c = conn or connect(db_path)
    done: list[dict] = []
    skipped: list[dict] = []
    try:
        # 统一收敛成 id 列表:url 与 id 都支持(老报告可能只有 url)
        keys: list[tuple[str, str]] = []
        for u in urls or []:
            keys.append(("url", str(u)))
        for i in ids or []:
            keys.append(("id", str(i)))
        if len(keys) > BULK_LIMIT:
            raise ValueError(f"一次最多处理 {BULK_LIMIT} 条,当前 {len(keys)} 条")

        seen: set[str] = set()
        for kind, key in keys:
            if not key or key in seen:
                continue
            seen.add(key)
            if kind == "url":
                row = c.execute(
                    "SELECT id, url, title FROM hot_topics WHERE url=? AND deleted=0", (key,)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT id, url, title FROM hot_topics WHERE id=? AND deleted=0", (key,)
                ).fetchone()
            if not row:
                skipped.append({"key": key,
                                "reason": "未找到该热点(可能已被废弃或被清理策略移除)"})
                continue
            _delete_and_log(c, row["id"], "manual", reason)
            done.append({"id": row["id"], "url": row["url"], "title": row["title"]})
        c.commit()
    finally:
        if own:
            c.close()
    return {"ok": True, "done": done, "skipped": skipped,
            "done_count": len(done), "skipped_count": len(skipped)}
