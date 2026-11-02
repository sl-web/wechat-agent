"""断点/运行日志:环节状态快照,支持断点续跑与全流程回溯(PRD 5.8)。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.core.circuit_breaker import new_run_id  # noqa: F401  重导出
from app.core.db import connect


@dataclass
class RunRecorder:
    """记录一次流程 run_id 下各环节的 checkpoint/日志。"""

    run_id: str
    db_path: str | None = None
    _conn: sqlite3.Connection | None = field(default=None, init=False)

    def __post_init__(self):
        self._conn = connect(self.db_path)

    def log(self, stage: str, status: str, detail: str = "") -> None:
        """status: success / failed / checkpoint

        ⚠️ **一律 INSERT,不覆盖历史行**(checkpoint 与 success/failed 都是)。
        曾经 success/failed 会 UPDATE 掉"同环节最后一条 finished_at 为空"的记录,
        那会把诊断信息一起吞掉:一次生成最终成功时,「第 n 次校验未通过:…」
        这条 checkpoint 会被 success **覆盖**,导致事后完全统计不出重试率,
        也让"为什么这次跑得特别慢"永远查不出来。运行日志本就该是只追加的。
        """
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        # checkpoint 是"进行中"的标记,不填 finished_at;success/failed 同时填上
        finished = None if status == "checkpoint" else now
        self._conn.execute(
            "INSERT INTO run_logs(run_id, stage, status, detail, started_at, finished_at) "
            "VALUES(?,?,?,?,?,?)",
            (self.run_id, stage, status, detail, now, finished),
        )
        self._conn.commit()

    def last_checkpoint(self, stage: str) -> bool:
        """该环节上次是否为 success checkpoint(用于断点续跑判断)。"""
        row = self._conn.execute(
            "SELECT status FROM run_logs WHERE run_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (self.run_id, stage),
        ).fetchone()
        return bool(row and row["status"] == "success")

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def trace_article(db_path: str | None, article_id: int) -> dict:
    """全链路回溯:热点→生成→推送→审核→回流 串联展示。"""
    conn = connect(db_path)
    try:
        article = conn.execute(
            "SELECT * FROM articles WHERE id=?", (article_id,)
        ).fetchone()
        if not article:
            return {}
        topic = conn.execute(
            "SELECT * FROM hot_topics WHERE id=?", (article["topic_id"],)
        ).fetchone()
        stats = conn.execute(
            "SELECT * FROM article_stats WHERE article_id=? ORDER BY stat_date", (article_id,)
        ).fetchall()
        return {
            "article": dict(article),
            "topic": dict(topic) if topic else None,
            "stats": [dict(s) for s in stats],
        }
    finally:
        conn.close()
