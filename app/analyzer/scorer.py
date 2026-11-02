"""热度评分器(PRD 5.2.1):先单平台归一化,再参数化加权。

公式:
  单平台归一化热度 = normalize(原始热度)   # percentile/log/minmax,按源分组
  综合热度 = Σ(来源权重 × 单平台归一化热度) × 时效衰减 × 领域相关度 × 传播系数

「传播系数」来自 `app/analyzer/growth.py` —— 用账号自己的历史转发数据学习标题形态。
⚠️ 它是**有界**的(默认 ±35%):几十篇的样本只够给弱信号,**绝不能**让它盖过热度与时效。
"""
from __future__ import annotations

import math
from datetime import datetime

from app.analyzer.growth import growth_score
from app.core.config import get_config


def normalize_percentile(values: list[float]) -> dict[int, float]:
    """排名百分位(0~1):数值越大排名越靠前。values 与索引对应。"""
    n = len(values)
    if n == 0:
        return {}
    ranked = sorted(range(n), key=lambda i: values[i], reverse=True)
    result = {}
    for pos, idx in enumerate(ranked):
        result[idx] = (n - pos) / n  # 第一名 = 1.0
    return result


def normalize_log(values: list[float]) -> dict[int, float]:
    """log10 归一化:log(1+x)/log(1+max)。"""
    if not values:
        return {}
    mx = max(values)
    if mx <= 0:
        return {i: 0.0 for i in range(len(values))}
    denom = math.log10(1 + mx)
    return {i: math.log10(1 + v) / denom for i, v in enumerate(values)}


def normalize_minmax(values: list[float]) -> dict[int, float]:
    """Min-Max 归一化。"""
    if not values:
        return {}
    lo, hi = min(values), max(values)
    if hi == lo:
        return {i: 1.0 for i in range(len(values))}
    return {i: (v - lo) / (hi - lo) for i, v in enumerate(values)}


NORMALIZERS = {
    "percentile": normalize_percentile,
    "log": normalize_log,
    "minmax": normalize_minmax,
}


def decay_factor(published_at: str | None, fetched_at: str | None, half_life_hours: float = 24.0) -> float:
    """时效衰减:半衰期指数衰减,返回 0~1。"""
    if not published_at or not fetched_at:
        return 1.0
    try:
        pub = datetime.strptime(published_at[:19], "%Y-%m-%d %H:%M:%S")
        now = datetime.strptime(fetched_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            pub = datetime.fromisoformat(published_at)
            now = datetime.fromisoformat(fetched_at)
        except ValueError:
            return 1.0
    hours = max(0.0, (now - pub).total_seconds() / 3600.0)
    if half_life_hours <= 0:
        return 1.0
    return 2 ** (-hours / half_life_hours)


def relevance_score(text: str, keywords: list[str]) -> float:
    """领域相关度:命中关键词数/关键词总数,0~1。"""
    if not keywords:
        return 1.0
    text_l = text.lower()
    hits = sum(1 for k in keywords if k.lower() in text_l)
    return hits / len(keywords)


class Scorer:
    """按数据源分组归一化,再综合加权。"""

    def __init__(self, config: dict | None = None):
        self.cfg = config or {}

    def _cfg(self, key: str, default=None):
        if self.cfg.get(key) is not None:
            return self.cfg[key]
        return get_config(key) or default

    def score_group(self, topics: list[dict]) -> list[dict]:
        """对一个抓取批次的全部热点(混合源)评分,原地写入 heat_score/rank_pct。"""
        if not topics:
            return topics

        method = self._cfg("normalize_method", "percentile")
        weights: dict = self._cfg("source_weight", {"wechat": 0.6, "douyin": 0.4})
        half_life = float(self._cfg("decay_half_life_hours", 24))
        keywords: list = self._cfg("keywords", [])

        # 传播潜力:用账号历史转发数据学出的画像给标题形态加权(见 analyzer/growth.py)
        virality_on = bool(self._cfg("virality_enabled", True))
        virality_w = float(self._cfg("virality_weight", 0.35) or 0)
        profile = self._cfg("growth_profile", {}) if virality_on else {}
        if not isinstance(profile, dict):
            profile = {}

        # 按来源分组归一化(解决跨平台量纲问题)
        by_source: dict[str, list[dict]] = {}
        for t in topics:
            by_source.setdefault(t.get("source", "unknown"), []).append(t)

        norm_by_source: dict[str, dict[int, float]] = {}
        for src, group in by_source.items():
            raw = [float(g.get("platform_hot") or 0) for g in group]
            norm = NORMALIZERS.get(method, normalize_percentile)(raw)
            norm_by_source[src] = norm

        total_weight = sum(weights.get(s, 0) for s in weights)
        for src, group in by_source.items():
            norms = norm_by_source[src]
            for i, t in enumerate(group):
                norm_val = norms.get(i, 0.0)
                w = weights.get(src, 0.0) / total_weight if total_weight else 0.0
                # 原文发布时间与抓取时间:兼容入库前(fetched_at/published_at)与入库后(pub_at/created_at)
                pub_time = t.get("pub_at") or t.get("published_at")
                fetch_time = t.get("fetched_at") or t.get("created_at")
                dk = decay_factor(pub_time, fetch_time, half_life)
                rel = relevance_score(
                    f"{t.get('title','')} {t.get('summary','')}", keywords
                ) if self._cfg("relevance_enabled", True) else 1.0
                vir = growth_score(t.get("title", ""), profile) if virality_on else 0.5
                # 传播系数:vir 0.5(与历史持平)→ 1.0;0 → 1-w;1 → 1+w
                gfactor = 1.0 + virality_w * (vir * 2 - 1) if (virality_on and virality_w) else 1.0
                t["rank_pct"] = round(norm_val, 4)
                t["virality"] = vir
                t["heat_score"] = round(w * norm_val * dk * rel * gfactor, 4)

        return topics


def score_batch(topics: list[dict], config: dict | None = None) -> list[dict]:
    return Scorer(config).score_group(topics)
