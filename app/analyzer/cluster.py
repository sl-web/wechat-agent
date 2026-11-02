"""聚类与主题提炼(PRD 5.2):基于关键词命中将热点归入主题簇。"""
from __future__ import annotations

from collections import OrderedDict

from app.analyzer.type_advisor import suggest_type


def cluster_by_keywords(topics: list[dict], keywords: list[str]) -> list[dict]:
    """按关键词把热点聚成主题簇。

    返回: [{topic, keyword, count, members: [...]}, ...]
    一个热点可命中多关键词,取命中数最多的主簇。
    """
    clusters: "OrderedDict[str, dict]" = OrderedDict()
    for t in topics:
        text = f"{t.get('title', '')} {t.get('summary', '')}"
        hits = [k for k in keywords if k.lower() in text.lower()]
        if not hits:
            hits = ["其他"]
        main = max(hits, key=len)  # 最长命中词作为主题(更具体)
        cl = clusters.setdefault(
            main,
            {"topic": main, "keyword": main, "count": 0, "heat_sum": 0.0, "members": []},
        )
        cl["count"] += 1
        cl["heat_sum"] += float(t.get("heat_score") or 0)
        cl["members"].append(t)

    # 按热度合计降序
    result = list(clusters.values())
    result.sort(key=lambda c: c["heat_sum"], reverse=True)
    return result


def _cluster_summary(c: dict, max_len: int = 220) -> str:
    """为该主题生成简短摘要:热度背景 + 成员报道要点拼装。"""
    topic = c["topic"]
    heat = round(c.get("heat_sum") or 0, 2)
    n = c["count"]
    # 取摘要/标题拼接成"报道要点",剔除"XX 消息/快讯"套话前缀
    import re

    points = []
    for m in c["members"][:6]:
        raw = (m.get("summary") or "").strip()
        raw = re.sub(r"^[【\[]?[\u4e00-\u9fa5A-Za-z0-9]{1,12}\s*\d{1,2}\s*月\s*\d{1,2}\s*日?消息[:：]?\s*", "", raw)
        raw = raw.replace("，", "。").split("。")[0].strip()
        # 摘要首句太短没信息量 → 回退用标题
        txt = raw if len(raw) >= 12 else (m.get("title") or "").strip()
        if txt and len(txt) >= 12 and txt not in points:
            points.append(txt)
    if not points:
        return f"该主题聚合了 {n} 条相关报道,综合热度 {heat}。"
    joined = "。".join(points[:3])
    if len(joined) > max_len:
        joined = joined[:max_len] + "…"
    return f"该主题聚合 {n} 条相关报道(综合热度 {heat}),报道要点包括:{joined}。"


def build_report(clusters: list[dict], top_n: int = 5) -> dict:
    """生成当日归纳报告:TopN 主题 + 建议选题。

    每个主题附:
      - summary: 简短聚类摘要(分析结果,供阅读)
      - members:  成员条目(标题/来源/链接/摘要),供展开细看
    """
    top = clusters[:top_n]
    report = {
        "date": "",
        "total_topics": sum(c["count"] for c in clusters),
        "clusters": len(clusters),
        "topics": [],
    }
    for c in top:
        best = max(c["members"], key=lambda m: float(m.get("heat_score") or 0), default={})
        members = [
            {
                "id": m.get("id"),
                "title": m.get("title"),
                "url": m.get("url"),
                "source": m.get("source"),
                "summary": (m.get("summary") or "")[:160],
                "heat": round(float(m.get("heat_score") or 0), 3),
                # 传播潜力(0~1,0.5 = 与账号历史平均持平):来自账号自己的转发数据画像。
                # 让选题时能看见"这条更像会被转发的标题形态",而不是只看热度。
                "virality": round(float(m.get("virality") or 0.5), 3),
            }
            for m in c["members"][:10]
        ]
        # 主题级传播潜力:取成员里最高的那条 —— 一个主题只要有一条"像会被转发"的报道就够了
        topic_vir = max((float(m.get("virality") or 0.5) for m in c["members"]), default=0.5)
        report["topics"].append(
            {
                "topic": c["topic"],
                "hot_count": c["count"],
                "heat_sum": round(c["heat_sum"], 2),
                "suggested_angle": f"围绕「{c['topic']}」展开事件回顾与影响解读",
                "summary": _cluster_summary(c),
                "brief": (best.get("summary") or "")[:80] if best else "",
                "members": members,
                "virality": round(topic_vir, 3),
                # 这个主题更适合哪种论证姿态 —— 用规则即时给出(零成本),
                # 用户勾完素材后还可以让大模型复核(尤其跨主题多选时)。
                "suggested_type": suggest_type(c["topic"], members),
                # sample 作为该主题的「代表选题」,生成环节用它作为主选题
                "sample": {
                    "id": best.get("id"),
                    "title": best.get("title"),
                    "url": best.get("url"),
                    "source": best.get("source"),
                    "summary": (best.get("summary") or "")[:300],
                },
            }
        )
    return report
