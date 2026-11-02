"""分析器测试:清洗/归一化评分/聚类/报告。"""
from __future__ import annotations

from app.analyzer.cleaner import Cleaner
from app.analyzer.cluster import build_report, cluster_by_keywords
from app.analyzer.scorer import (
    NORMALIZERS,
    decay_factor,
    normalize_log,
    normalize_minmax,
    normalize_percentile,
    relevance_score,
    score_batch,
)

KW = ["大模型", "芯片", "自动驾驶"]


def test_cleaner_filters():
    cleaner = Cleaner()
    items = [
        {"title": "扫码领福利大模型课程", "summary": "广告"},
        {"title": "震惊!大模型颠覆一切", "summary": ""},
        {"title": "某厂发布新芯片", "summary": "性能提升"},
    ]
    out = cleaner.clean(items)
    assert len(out) == 1
    assert out[0]["title"] == "某厂发布新芯片"


def test_normalize_percentile_ranks():
    values = [10.0, 100.0, 50.0]
    norm = normalize_percentile(values)
    # 最高值(100)得 1.0
    assert norm[1] == 1.0
    # 单调
    assert norm[1] > norm[2] > norm[0]


def test_normalize_log_bounds():
    values = [0.0, 1000.0]
    norm = normalize_log(values)
    assert 0 <= norm[0] <= 1 and norm[1] == 1.0


def test_normalize_minmax():
    values = [1.0, 3.0]
    norm = normalize_minmax(values)
    assert norm[0] == 0.0 and norm[1] == 1.0


def test_all_normalizers_available():
    assert set(NORMALIZERS) == {"percentile", "log", "minmax"}


def test_decay_factor():
    # 无时间信息不衰减
    assert decay_factor(None, None) == 1.0
    # 同刻发布不衰减
    assert decay_factor("2026-09-09 08:00:00", "2026-09-09 08:00:00") == 1.0
    # 超过一个半衰期(24h)衰减到 0.5
    assert abs(decay_factor("2026-09-08 08:00:00", "2026-09-09 08:00:00", 24) - 0.5) < 0.01


def test_relevance_score():
    assert relevance_score("大模型 自动驾驶", KW) == 2 / 3
    assert relevance_score("无关内容", KW) == 0.0


def test_score_batch_cross_source_normalization():
    """跨源归一化:微信阅读量 1000 vs 抖音 1e7,评分不受量纲主导。"""
    topics = [
        {"source": "wechat", "platform_hot": 100000.0, "title": "微信大模型热文",
         "summary": "大模型", "published_at": "2026-09-09 08:00:00",
         "fetched_at": "2026-09-09 09:00:00"},
        {"source": "douyin", "platform_hot": 50000000.0, "title": "抖音大模型热点",
         "summary": "大模型", "published_at": "2026-09-09 08:00:00",
         "fetched_at": "2026-09-09 09:00:00"},
        {"source": "wechat", "platform_hot": 50.0, "title": "微信低热度",
         "summary": "大模型", "published_at": "2026-09-09 08:00:00",
         "fetched_at": "2026-09-09 09:00:00"},
    ]
    cfg = {"normalize_method": "percentile",
           "source_weight": {"wechat": 0.6, "douyin": 0.4},
           "decay_half_life_hours": 24, "keywords": ["大模型"]}
    out = score_batch(topics, cfg)
    # 微信内高热度那条应是本组第一
    w_group = [t for t in out if t["source"] == "wechat"]
    assert w_group[0]["heat_score"] > w_group[1]["heat_score"]
    # 抖音大数值不得碾压:热度应被归一化到 0~1 级
    assert all(0 <= t["heat_score"] <= 1 for t in out)


def test_cluster_and_report():
    topics = [
        {"title": "新芯片发布", "summary": "芯片工艺", "heat_score": 0.8, "url": "u1", "source": "wechat"},
        {"title": "芯片巨头官宣", "summary": "芯片", "heat_score": 0.6, "url": "u2", "source": "wechat"},
        {"title": "大模型新能力", "summary": "大模型", "heat_score": 0.5, "url": "u3", "source": "douyin"},
    ]
    clusters = cluster_by_keywords(topics, KW)
    assert len(clusters) == 2  # 芯片 + 大模型
    report = build_report(clusters, top_n=5)
    assert report["total_topics"] == 3
    assert report["topics"][0]["topic"] == "芯片"
