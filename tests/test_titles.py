"""标题候选的测试(用注入的假 LLM,不打网络、不读全局画像)。"""
from __future__ import annotations

import json

import pytest

from app.generator.thesis import GenerationError
from app.generator.titles import _clean_title, build_prompt, suggest

_ARTICLE = {
    "title": "算力成本转嫁的财务账",
    "thesis": "这轮涨价的本质是把推理成本转嫁给高付费用户。",
    "lead": "发布会讲的是技术普惠。",
    "quote": "把算力成本转嫁给付费用户,才是这轮涨价真正的动作。",
    "body": "正文内容" * 50,
}


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat(self, messages, **kwargs):
        return self.payload if isinstance(self.payload, str) else json.dumps(
            self.payload, ensure_ascii=False)


def test_suggest_parses_and_filters():
    """过滤过短/重复,并剥掉模型包在外面的引号。"""
    llm = _FakeLLM({"titles": [
        {"title": "为什么涨价总由付费用户买单？", "form": "疑问式", "reason": "有代入感"},
        {"title": "短", "form": "疑问式", "reason": "太短,应被丢弃"},
        {"title": "为什么涨价总由付费用户买单？", "form": "疑问式", "reason": "重复,应被丢弃"},
        {"title": "「把成本转嫁给谁,才是这轮涨价的真问题」", "form": "反常识", "reason": "去引号"},
    ]})
    r = suggest(_ARTICLE, n=3, llm=llm, profile={})
    titles = [t["title"] for t in r["titles"]]
    assert titles == ["为什么涨价总由付费用户买单？", "把成本转嫁给谁,才是这轮涨价的真问题"]
    assert r["titles"][0]["form"] == "疑问式"
    assert r["profile_used"] is False       # 未传画像 → 全是中性分


def test_suggest_annotates_virality_from_profile():
    llm = _FakeLLM({"titles": [
        {"title": "为什么天气变冷了？", "form": "疑问式", "reason": "x"},
        {"title": "某公司发布新产品", "form": "判断式", "reason": "y"},
    ]})
    from app.analyzer.growth import build_profile

    prof = build_profile([{"title": "为什么天气变冷了？", "stats": {"read_count": 100, "share_count": 10}}] * 5
                         + [{"title": "某公司发布新产品", "stats": {"read_count": 100, "share_count": 0}}] * 5)
    r = suggest(_ARTICLE, n=3, llm=llm, profile=prof)
    scores = {t["title"]: t["virality"] for t in r["titles"]}
    assert r["profile_used"] is True
    assert scores["为什么天气变冷了？"] > 0.5
    assert scores["某公司发布新产品"] == 0.5      # 没有钩子 → 中性,不扣分


def test_suggest_handles_array_as_string():
    """模型把数组写成字符串时的兜底(thesis 那边也踩过同样的坑)。"""
    inner = json.dumps([{"title": "把成本转嫁给谁才是真问题", "form": "反常识", "reason": "z"}],
                       ensure_ascii=False)
    llm = _FakeLLM({"titles": inner})
    r = suggest(_ARTICLE, n=3, llm=llm, profile={})
    assert r["titles"][0]["title"] == "把成本转嫁给谁才是真问题"


def test_suggest_raises_on_garbage():
    with pytest.raises(GenerationError):
        suggest(_ARTICLE, n=3, llm=_FakeLLM({"titles": []}), profile={})
    with pytest.raises(GenerationError):
        suggest({"title": "", "body": ""}, n=3, llm=_FakeLLM({"titles": []}), profile={})


def test_clean_title_strips_wrappers():
    assert _clean_title("「标题」") == "标题"
    assert _clean_title('"标题"') == "标题"
    assert _clean_title("《标题》") == "标题"
    assert _clean_title("普通标题") == "普通标题"


def test_prompt_carries_anti_clickbait_rule():
    """标题候选不能变成标题党 —— 失真会被限流,反而降低打开率。"""
    sys_p, user = build_prompt(_ARTICLE, 3)
    assert "标题党" in sys_p and "震惊" in sys_p
    assert "形态必须不同" in sys_p
    assert _ARTICLE["thesis"] in user
