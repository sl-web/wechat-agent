"""分析归纳编排:清洗 → 评分 → 聚类 → 报告(PRD 流程 ②)。"""
from __future__ import annotations

from datetime import datetime

from app.analyzer.cleaner import Cleaner
from app.analyzer.cluster import build_report, cluster_by_keywords
from app.analyzer.scorer import score_batch
from app.core.config import get_all_config
from app.core.db import connect


def analyze(topics: list[dict], config: dict | None = None) -> dict:
    """输入:抓取入库的热点 dict 列表。
    输出: {"cleaned": [...], "scored": [...], "clusters": [...], "report": {...}}
    """
    cfg = config or get_all_config()
    keywords: list = cfg.get("keywords", [])

    cleaned = Cleaner().clean(topics)
    scored = score_batch(cleaned, cfg)
    clusters = cluster_by_keywords(scored, keywords)
    report = build_report(clusters, top_n=cfg.get("top_n", 5) // 4 or 5)

    return {
        "cleaned": cleaned,
        "scored": scored,
        "clusters": clusters,
        "report": report,
    }


def persist_scores(scored: list[dict], db_path: str | None = None) -> int:
    """把评分结果写回 hot_topics(heat_score/rank_pct/analyzed_at/first_analyzed_at),返回更新条数。

    scored 为 analyze() 输出中的 scored 列表;按热点 id 匹配更新。
    保证看板上"热度"来源一致,分析结果可被看板直接读取。

    两个时间字段分工不同,**别混用**:
    - `analyzed_at`       = 最近一次被分析的时刻,每次分析都刷新;
    - `first_analyzed_at` = **首次**被分析的时刻,`COALESCE(...)` 只写一次、永不覆盖。
      「今日分析」按它统计 —— 否则每次重跑分析都会把历史条目算进今天,
      数字直接等于库里未删除热点的总数(真实踩过)。
    """
    own = db_path is None
    conn = connect(db_path) if db_path else connect()
    updated = 0
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for t in scored:
            tid = t.get("id")
            if tid is None:
                continue
            cur = conn.execute(
                """UPDATE hot_topics
                   SET heat_score=?, rank_pct=?, analyzed_at=?,
                       first_analyzed_at=COALESCE(first_analyzed_at, ?)
                   WHERE id=? AND deleted=0""",
                (t.get("heat_score"), t.get("rank_pct"), now, now, tid),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        if own:
            conn.close()


def report_for_range(range_key: str | None = None, db_path: str | None = None,
                     today: str | None = None) -> dict:
    """按**时间范围实时**聚类出一份报告(只读:不落库、不改分数、不写 config)。

    为什么是"实时聚类"而不是过滤历史快照:
    快照里的聚类是对**全部**热点算出来的,而 `members` 只截取了前 10 条 ——
    想按时间过滤成员,就算不出该主题在时段内的真实条数(会严重偏小),
    计数字段和列表会对不上。而聚类本身只要几毫秒(实测 4~55ms,不调 LLM),
    所以直接按范围重算,数字天然一致。
    """
    from app.core.daterange import normalize_range, range_bounds, range_label
    from app.core.db import connect

    key = normalize_range(range_key)
    lo, hi = range_bounds(key, datetime.strptime(today, "%Y-%m-%d").date() if today else None)

    own = db_path is None
    conn = connect(db_path) if db_path else connect()
    try:
        sql = "SELECT * FROM hot_topics WHERE deleted=0"
        params: list = []
        if lo and hi:
            # 用 fetch_date(进入系统的那天)而不是 pub_at(原文发布时间) ——
            # pub_at 常为空,且"今天看到的信息"才是用户想看的维度,与「今日抓取」同源。
            sql += " AND date(fetch_date) BETWEEN ? AND ?"
            params += [lo, hi]
        sql += " ORDER BY id DESC LIMIT 200"
        topics = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        if own:
            conn.close()

    if not topics:
        return {"report": None, "run_id": None, "range": key, "range_label": range_label(key),
                "source_count": 0, "live": True,
                "empty_reason": "该时间范围内没有热点素材(换个范围或先抓取)"}
    result = analyze(topics)
    return {"report": result["report"], "run_id": None, "range": key,
            "range_label": range_label(key), "source_count": len(topics),
            "live": True, "empty_reason": ""}
