"""运营看板聚合查询 + 告警(PRD 5.10)。"""
from __future__ import annotations

import sqlite3

from app.core.db import connect


def dashboard_summary(db_path: str | None = None, today: str | None = None) -> dict:
    """看板总览指标。today 默认今天(YYYY-MM-DD),可用 hot_topics.fetch_date。"""
    conn = connect(db_path) if db_path else connect()
    try:
        def one(sql, params=()):
            r = conn.execute(sql, params).fetchone()
            return r[0] if r else 0

        d = today or conn.execute("SELECT date('now','localtime')").fetchone()[0]
        summary = {
            "date": d,
            "fetch_count": one("SELECT COUNT(*) FROM hot_topics WHERE deleted=0 AND fetch_date=?", (d,)),
            # 今日分析 = 今天**首次**被分析的条目。
            # ⚠️ 不能用 analyzed_at:它每次分析都被刷新,重跑一次分析就会把历史条目全算进今天,
            # 数字直接变成"库里热点总数"(用户报过这个 bug)。
            "analyzed_today": one(
                "SELECT COUNT(*) FROM hot_topics WHERE deleted=0 AND date(first_analyzed_at)=?", (d,)
            ),
            # 今日生成 = 今日生成且**尚未审核**的稿件(status='generated')
            "generated_today": one(
                "SELECT COUNT(*) FROM articles WHERE date(created_at)=? AND status='generated'", (d,)
            ),
            # 待处理稿件 = 已通过审核、**尚未推送草稿**的稿件(status='approved')
            "pending_review": one(
                "SELECT COUNT(*) FROM articles WHERE status='approved'"
            ),
            "published_total": one("SELECT COUNT(*) FROM articles WHERE status IN ('published','backfilled')"),
            "backfilled_total": one("SELECT COUNT(*) FROM article_stats"),
            "blacklist_or_fail": one("SELECT COUNT(*) FROM articles WHERE status='generation_failed'"),
        }
        # 效果 Top
        top_rows = conn.execute(
            """SELECT a.id, a.title, s.read_count, s.like_count, s.share_count
               FROM article_stats s JOIN articles a ON a.id=s.article_id
               ORDER BY s.read_count DESC LIMIT 5"""
        ).fetchall()
        summary["top_articles"] = [dict(r) for r in top_rows]

        # 熔断/失败分布
        summary["fail_by_stage"] = [dict(r) for r in conn.execute(
            "SELECT stage, COUNT(*) AS n FROM run_logs WHERE status='failed' GROUP BY stage"
        ).fetchall()]

        # 最近运行记录(看板流水线反馈,含每次执行的结果摘要)
        summary["recent_runs"] = [dict(r) for r in conn.execute(
            """SELECT id, run_id, stage, status, detail, started_at
               FROM run_logs
               WHERE stage IN ('fetch','analyze','generate','push','backfill','clean')
                 AND status IN ('success','failed')
               ORDER BY id DESC LIMIT 8"""
        ).fetchall()]

        # 今日任务链进度:抓取 → 分析 → 生成 → 人工审核 → 发布/回流
        summary["chain"] = _chain_progress(conn, d)
        return summary
    finally:
        conn.close()


def _chain_progress(conn, d: str) -> list[dict]:
    """任务链各环节的「今日产出 + 最近一次运行状态」,供看板进度条展示。"""

    def last(stage: str) -> dict | None:
        r = conn.execute(
            """SELECT status, detail, started_at, finished_at FROM run_logs
               WHERE stage=? AND status IN ('success','failed')
               ORDER BY id DESC LIMIT 1""",
            (stage,),
        ).fetchone()
        return dict(r) if r else None

    def count(sql, params=()) -> int:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else 0

    return [
        {
            "key": "fetch", "label": "抓取热点", "unit": "条",
            "count": count("SELECT COUNT(*) FROM hot_topics WHERE deleted=0 AND fetch_date=?", (d,)),
            "last": last("fetch"),
        },
        {
            "key": "analyze", "label": "分析归纳", "unit": "条",
            "count": count("SELECT COUNT(*) FROM hot_topics WHERE deleted=0 AND date(first_analyzed_at)=?", (d,)),
            "last": last("analyze"),
        },
        {
            # 与「今日生成」指标同口径:今日生成且未审核
            "key": "generate", "label": "今日生成(未审核)", "unit": "篇",
            "count": count(
                "SELECT COUNT(*) FROM articles WHERE date(created_at)=? AND status='generated'", (d,)
            ),
            "last": last("generate"),
        },
        {
            "key": "review", "label": "待审核(累计)", "unit": "篇",
            "count": count(
                "SELECT COUNT(*) FROM articles WHERE status IN ('generated','edited')"
            ),
            "last": None,
        },
        {
            # 与「待处理稿件」指标同口径:已通过审核待推送
            "key": "push", "label": "待推送(已审核)", "unit": "篇",
            "count": count("SELECT COUNT(*) FROM articles WHERE status='approved'"),
            "last": last("push"),
        },
        {
            "key": "publish", "label": "已发布/回流", "unit": "篇",
            "count": count("SELECT COUNT(*) FROM articles WHERE status IN ('published','backfilled')"),
            "last": last("backfill"),
        },
    ]


def review_stale(db_path: str | None = None, stale_hours: int = 24) -> list[dict]:
    """滞留超时的稿件(用于告警)。

    告警对象是「卡在人工环节」的稿件:待审核(generated/edited)、
    已审核待推送(approved)、已推送待确认发布(pushed)。
    终态(published/backfilled/rejected/generation_failed)不告警。
    """
    conn = connect(db_path) if db_path else connect()
    try:
        rows = conn.execute(
            """SELECT id, title, status, created_at FROM articles
               WHERE status IN ('generated','edited','approved','pushed')
                 AND datetime(created_at) <= datetime('now','localtime', ?)""",
            (f"-{stale_hours} hours",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- 指标点击详情 ----
# 每个指标: SQL 查询 + 列定义(用于前端表格展示)

_METRIC_QUERIES: dict[str, dict] = {
    "fetch_today": {
        "title": "今日抓取的热点",
        "sql": """SELECT h.id, h.source, h.title, h.platform_hot, h.fetch_mode, h.url, h.created_at
                  FROM hot_topics h WHERE h.deleted=0 AND h.fetch_date=? ORDER BY h.platform_hot DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "source", "label": "来源"},
            {"key": "title", "label": "标题"},
            {"key": "platform_hot", "label": "热度"},
            {"key": "fetch_mode", "label": "模式"},
            {"key": "url", "label": "链接", "link": True},
        ],
        "params": ("date",),
    },
    "analyzed_today": {
        "title": "今日分析的热点",
        "sql": """SELECT h.id, h.source, h.title, h.heat_score, h.rank_pct,
                         h.category, h.first_analyzed_at, h.analyzed_at
                  FROM hot_topics h
                  WHERE h.deleted=0 AND date(h.first_analyzed_at)=?
                  ORDER BY h.heat_score DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "source", "label": "来源"},
            {"key": "title", "label": "标题"},
            {"key": "heat_score", "label": "综合热度"},
            {"key": "rank_pct", "label": "归一化"},
            {"key": "category", "label": "分类"},
            {"key": "first_analyzed_at", "label": "首次分析时间"},
            {"key": "analyzed_at", "label": "最近分析时间"},
        ],
        "params": ("date",),
    },
    "generated_today": {
        "title": "今日生成(未审核)",
        "note": "口径:今日生成且尚未进入审核流程的稿件(status=generated)",
        "sql": """SELECT a.id, a.title, a.status, a.structure_id, a.fact_notes, a.created_at
                  FROM articles a
                  WHERE date(a.created_at)=? AND a.status='generated'
                  ORDER BY a.id DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "title", "label": "标题"},
            {"key": "status", "label": "状态", "badge": "status"},
            {"key": "structure_id", "label": "结构"},
            {"key": "created_at", "label": "生成时间"},
        ],
        "params": ("date",),
    },
    "pending_review": {
        "title": "待处理稿件(已通过审核,待推送草稿)",
        "note": "口径:审核通过但尚未推送草稿的稿件(status=approved)",
        "sql": """SELECT a.id, a.title, a.status, a.draft_url, h.title AS topic_title,
                         a.created_at
                  FROM articles a LEFT JOIN hot_topics h ON a.topic_id=h.id
                  WHERE a.status='approved'
                  ORDER BY a.created_at DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "title", "label": "标题"},
            {"key": "topic_title", "label": "来源热点"},
            {"key": "status", "label": "状态", "badge": "status"},
            {"key": "created_at", "label": "时间"},
        ],
        "params": (),
    },
    "generation_failed": {
        "title": "生成失败 / 熔断稿件",
        "sql": """SELECT a.id, a.title, a.reject_reason, a.fail_count, a.created_at
                  FROM articles a WHERE a.status='generation_failed'
                  ORDER BY a.created_at DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "title", "label": "标题"},
            {"key": "reject_reason", "label": "失败原因"},
            {"key": "fail_count", "label": "失败次数"},
        ],
        "params": (),
    },
    "published": {
        "title": "累计已发布",
        "sql": """SELECT a.id, a.title, a.published_at, a.draft_url, a.created_at
                  FROM articles a WHERE a.status IN ('published','backfilled')
                  ORDER BY a.published_at DESC LIMIT ?""",
        "cols": [
            {"key": "id", "label": "ID"},
            {"key": "title", "label": "标题"},
            {"key": "published_at", "label": "发布时间"},
            {"key": "draft_url", "label": "草稿链接", "link": True},
        ],
        "params": (),
    },
    "backfilled": {
        "title": "效果回流数据",
        "sql": """SELECT a.id AS article_id, a.title AS article_title, s.read_count,
                         s.like_count, s.share_count, s.favorite_count, s.stat_date
                  FROM article_stats s LEFT JOIN articles a ON a.id=s.article_id
                  ORDER BY s.stat_date DESC, s.id DESC LIMIT ?""",
        "cols": [
            {"key": "article_id", "label": "文章ID"},
            {"key": "article_title", "label": "文章"},
            {"key": "read_count", "label": "阅读"},
            {"key": "like_count", "label": "点赞"},
            {"key": "share_count", "label": "转发"},
            {"key": "stat_date", "label": "统计日"},
        ],
        "params": (),
    },
}

_DEFAULT_LIMIT = 200


def metric_detail(metric: str, today: str | None = None,
                  db_path: str | None = None, limit: int = _DEFAULT_LIMIT) -> dict:
    """查询单个指标的明细数据,返回 {title, cols, rows} 供前端表格渲染。"""
    if metric not in _METRIC_QUERIES:
        raise KeyError(f"未知指标: {metric}")
    spec = _METRIC_QUERIES[metric]
    conn = connect(db_path) if db_path else connect()
    try:
        d = today or conn.execute("SELECT date('now','localtime')").fetchone()[0]
        params = []
        for p in spec.get("params", ()):
            params.append(d if p == "date" else p)
        params.append(limit)
        rows = conn.execute(spec["sql"], tuple(params)).fetchall()
        return {
            "metric": metric,
            "title": spec["title"],
            "note": spec.get("note", ""),
            "cols": spec["cols"],
            "rows": [dict(r) for r in rows],
            "total": len(rows),
        }
    finally:
        conn.close()


def metric_list() -> list[str]:
    return list(_METRIC_QUERIES.keys())
