"""传播画像(把回流数据接回选题)与发布时段建议的测试。

全部离线:画像用合成数据,时段用固定时间点。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.analyzer.growth import (
    _terms,
    build_profile,
    growth_score,
)
from app.core.publish_window import parse_window, status


# ---------------- 传播画像 ----------------

def _items(titles_shares: list[tuple[str, int]]) -> list[dict]:
    """构造发表记录:{标题: 分享数},统一按 100 阅读算,便于控制转发率。"""
    return [{"title": t, "stats": {"read_count": 100, "share_count": s}}
            for t, s in titles_shares]


# 5 篇疑问式(转发率 10%) + 3 篇普通(0%) + 2 篇破折号(0%)
_SAMPLE = _items(
    [("为什么天气变冷了？", 10)] * 5
    + [("某公司发布新产品", 0)] * 3
    + [("行业观察——细节待定", 0)] * 2
)


def test_build_profile_needs_enough_samples():
    """样本太少就不学 —— 学了也不可信,不如明确说"没画像"。"""
    p = build_profile(_items([("为什么天气变冷了？", 1)] * 5))
    assert p["ok"] is False and "不足以学习" in p["reason"]


def test_build_profile_learns_question_form():
    p = build_profile(_SAMPLE)
    assert p["ok"] is True and p["samples"] == 10
    forms = {f["key"]: f for f in p["forms"]}
    assert forms["question"]["n"] == 5
    assert forms["question"]["lift"] == pytest.approx(2.0, abs=0.05)   # 0.10 / 0.05


def test_profile_excludes_forms_with_too_few_samples():
    """只出现 2 篇的形态(破折号)不参与打分 —— 样本不够,纯噪声。"""
    p = build_profile(_SAMPLE)
    keys = {f["key"] for f in p["forms"]}
    assert "reveal" not in keys


def test_terms_drop_function_char_fragments():
    """真实踩过:从「别人的因果」切出 `人的`,lift 高得离谱却毫无意义。"""
    got = _terms("别人的因果")
    assert "人的" not in got and "别的" not in got
    assert "别人" in got and "因果" in got


def test_growth_score_neutral_without_profile():
    assert growth_score("为什么天气变冷了？", {}) == 0.5
    assert growth_score("为什么天气变冷了？", None) == 0.5
    assert growth_score("为什么天气变冷了？", {"ok": False}) == 0.5


def test_growth_score_boosts_matching_hook():
    p = build_profile(_SAMPLE)
    assert growth_score("为什么天气变冷了？", p) > 0.5


def test_growth_score_does_not_punish_missing_hook():
    """⚠️ 核心约定:**没有正向钩子 = 中性 0.5,不扣分**。

    负向 lift 在几十篇的样本量下不可靠,拿它扣分会把本来会被转发的标题打下去 ——
    和「一条会误伤的护栏,代价比它挡下的风险还大」是同一个教训。
    """
    p = build_profile(_SAMPLE)
    assert growth_score("某公司发布新产品", p) == 0.5
    assert growth_score("行业观察——细节待定", p) == 0.5


def test_growth_score_only_looks_at_title():
    """⚠️ 只看标题,**不接受摘要** —— 用函数签名把这个决定钉死。

    为什么:画像本来就从标题学出来,拿学到的词去匹配摘要会大量误判 ——
    实测「学会」在科技摘要里是"学会使用",与人生类标题里的语境完全不同,
    结果 100 条成员里 14 条被错误加分。精度优先于召回。
    """
    import inspect

    from app.analyzer import growth

    assert list(inspect.signature(growth.growth_score).parameters) == ["title", "profile"]


def test_scorer_virality_is_bounded_and_switchable():
    """传播系数必须有界,且关掉后热度**完全不变**。"""
    from app.analyzer.scorer import score_batch

    prof = build_profile(_SAMPLE)
    topics = [
        {"id": 1, "source": "rss", "title": "为什么天气变冷了？", "summary": "", "platform_hot": 100},
        {"id": 2, "source": "rss", "title": "某公司发布新产品", "summary": "", "platform_hot": 100},
    ]
    base = {"keywords": [], "normalize_method": "percentile", "source_weight": {"rss": 1},
            "decay_half_life_hours": 0, "relevance_enabled": False}

    off = score_batch([dict(t) for t in topics], {**base, "virality_enabled": False})
    on = score_batch([dict(t) for t in topics],
                     {**base, "virality_enabled": True, "virality_weight": 0.35,
                      "growth_profile": prof})

    # 关掉时 virality 一律 0.5(中性),热度与老公式一致
    assert [t["virality"] for t in off] == [0.5, 0.5]
    # 打开时:有钩子的那篇被抬高,且不超过 ±35%
    assert on[0]["heat_score"] > off[0]["heat_score"]
    assert on[0]["heat_score"] <= off[0]["heat_score"] * 1.35 + 1e-6
    # 没钩子的那篇不变
    assert on[1]["heat_score"] == pytest.approx(off[1]["heat_score"])


def test_scorer_weight_zero_equals_disabled():
    """权重设 0 应等价于关闭 —— 避免"开了但没效果"的困惑。"""
    from app.analyzer.scorer import score_batch

    prof = build_profile(_SAMPLE)
    topics = [{"id": 1, "source": "rss", "title": "为什么天气变冷了？", "summary": "",
               "platform_hot": 100}]
    base = {"keywords": [], "normalize_method": "percentile", "source_weight": {"rss": 1},
            "decay_half_life_hours": 0, "relevance_enabled": False}
    off = score_batch([dict(t) for t in topics], {**base, "virality_enabled": False})
    zero = score_batch([dict(t) for t in topics],
                       {**base, "virality_enabled": True, "virality_weight": 0,
                        "growth_profile": prof})
    assert zero[0]["heat_score"] == pytest.approx(off[0]["heat_score"])


# ---------------- 发布时段 ----------------

def test_parse_window_skips_bad_segments():
    """配置写错一个字不该让功能整体不可用 —— 坏的片段直接跳过。"""
    assert parse_window("07:00-09:00, 乱写, 20:00-22:00") == [(420, 540), (1200, 1320)]
    assert parse_window("09:00-07:00") == []          # 结束早于开始 → 丢弃
    assert parse_window("") == []


def test_publish_window_status():
    spec = "07:00-09:00, 12:00-13:00, 20:00-22:00"
    inside = status(spec, now=datetime(2026, 9, 11, 8, 30))
    assert inside["in_window"] is True and inside["current"] == "07:00-09:00"

    before = status(spec, now=datetime(2026, 9, 11, 11, 0))
    assert before["in_window"] is False
    assert before["next"]["window"] == "12:00-13:00" and before["next"]["minutes_until"] == 60

    # 今天时段都过了 → 指向明天的第一个时段(跨零点计算)
    late = status(spec, now=datetime(2026, 9, 11, 23, 30))
    assert late["in_window"] is False
    assert late["next"]["window"] == "07:00-09:00" and late["next"]["minutes_until"] == 450


def test_publish_window_empty_spec_is_graceful():
    s = status("", now=datetime(2026, 9, 11, 8, 30))
    assert s["in_window"] is False and s["windows"] == []
    assert "停用" in s["hint"]
