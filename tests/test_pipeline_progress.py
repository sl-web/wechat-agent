"""流水线进度状态 + 采集器诊断测试。

覆盖两类回归:
1. 进度状态机(begin/mark/finish/snapshot)能如实反映阶段与失败原因;
2. 采集器逐源诊断 + 过滤漏斗,保证"抓不到"时能给出具体原因而非笼统空话。
"""
from __future__ import annotations

from app import pipeline_state as ps
from app.collectors.base import BaseHotTopic
from app.collectors.rss import RssCollector


# ---------- 进度状态 ----------

def test_progress_snapshot_lifecycle():
    ps.begin("fetch", "run-1")
    s = ps.snapshot()
    assert s["running"] is True
    assert s["stage"] == "fetch"
    assert s["label"] == "抓取热点"
    assert s["steps"][0]["status"] == "pending"

    ps.mark("fetch", "running", "连接 RSS 源…")
    ps.mark("fetch", "success", "抓到 10 条,新入库 3 条")
    s = ps.snapshot()
    assert s["steps"][0]["status"] == "success"
    assert s["logs"][-1]["detail"] == "抓到 10 条,新入库 3 条"

    ps.finish(True, result={"fetched": 3})
    s = ps.snapshot()
    assert s["running"] is False
    assert s["ok"] is True
    assert s["elapsed"] >= 0
    assert s["result"] == {"fetched": 3}


def test_all_stage_expands_to_full_chain():
    ps.begin("all", "run-2")
    s = ps.snapshot()
    assert [x["stage"] for x in s["steps"]] == ["fetch", "analyze", "generate"]


def test_finish_records_error_and_hint():
    ps.begin("generate", "run-3")
    ps.finish(False, error="生成失败:LLM 调用失败: Connection error.",
              hint="无法连接 LLM 服务")
    s = ps.snapshot()
    assert s["ok"] is False
    assert "Connection error" in s["error"]
    assert s["hint"] == "无法连接 LLM 服务"


# ---------- 采集器诊断 ----------

def test_source_diag_records_failure():
    """源不可达时必须留下真实异常,供上层生成可读原因。"""
    col = RssCollector(
        keywords=[], top_n=5,
        feeds=[{"name": "坏源", "url": "http://127.0.0.1:9/not-exist", "enabled": True}],
    )
    items = col.fetch()
    assert items == []
    assert len(col.source_diag) == 1
    d = col.source_diag[0]
    assert d["ok"] is False
    assert d["name"] == "坏源"
    assert d["error"]  # 非空的具体错误信息


def test_source_diag_records_success(monkeypatch):
    """源正常时记录条数与耗时,便于展示"逐源抓到多少"。"""
    rss = (
        '<?xml version="1.0"?><rss><channel>'
        "<item><title>AI 新闻一</title><link>http://x/1</link></item>"
        "</channel></rss>"
    )
    col = RssCollector(
        keywords=[], top_n=5,
        feeds=[{"name": "好源", "url": "http://x/feed", "enabled": True}],
    )
    monkeypatch.setattr(col, "_http_get", lambda url, timeout=15: rss)
    items = col.fetch()
    assert len(items) == 1
    d = col.source_diag[0]
    assert d["ok"] is True
    assert d["count"] == 1
    assert d["name"] == "好源"


def test_collect_funnel_counts(monkeypatch):
    """漏斗计数:原始 → 去重 → 关键词 → 截断,便于定位"抓到但入库 0"。"""
    col = RssCollector(keywords=["AI"], top_n=10, feeds=[])
    monkeypatch.setattr(
        col, "fetch",
        lambda mode="auto": [
            BaseHotTopic(source="rss", title="AI 大模型最新进展"),
            BaseHotTopic(source="rss", title="与主题无关的新闻"),
            BaseHotTopic(source="rss", title="AI 大模型最新进展"),  # 标题重复
        ],
    )
    out = col.collect()
    assert len(out) == 1
    assert col.funnel == {"raw": 3, "deduped": 2, "filtered": 1, "kept": 1}


def test_parse_feed_rejects_non_rss():
    """返回 HTML/空内容时给出明确错误,而不是让 ET 抛难懂的 ParseError。"""
    col = RssCollector(feeds=[])
    try:
        col._parse_feed("某源", "<html><body>403 Forbidden</body></html>")
    except ValueError as e:
        assert "不是 RSS" in str(e)
    else:  # pragma: no cover
        raise AssertionError("应抛出 ValueError")

    try:
        col._parse_feed("某源", "   ")
    except ValueError as e:
        assert "响应为空" in str(e)
    else:  # pragma: no cover
        raise AssertionError("空响应应抛出 ValueError")
