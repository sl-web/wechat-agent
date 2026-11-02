"""思考模式开关的测试(实测依据:思考占掉一次生成约 90% 的耗时)。"""
from __future__ import annotations

import pytest

from app.core.settings import settings
from app.generator.llm import LLMClient

DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
OPENAI = "https://api.openai.com/v1"


class _Msg:
    def __init__(self, content="{}"):
        self.content = content


class _Choice:
    def __init__(self):
        self.message = _Msg()


class _Resp:
    def __init__(self):
        self.choices = [_Choice()]


class _FakeCompletions:
    def __init__(self, sink):
        self.sink = sink

    def create(self, **kwargs):
        self.sink.update(kwargs)
        return _Resp()


def _fake_client(sink: dict):
    class _C:
        def __init__(self):
            self.chat = type("chat", (), {"completions": _FakeCompletions(sink)})()
    return _C()


def _client(monkeypatch, base_url: str, model: str, thinking: str) -> LLMClient:
    monkeypatch.setattr(settings, "llm_thinking", thinking)
    c = LLMClient(base_url=base_url, api_key="sk-test", model=model)
    return c


# ---------- 开关判定 ----------

def test_auto_disables_thinking_for_dashscope(monkeypatch):
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "auto")
    assert c.should_disable_thinking() is True


def test_auto_does_not_touch_other_providers(monkeypatch):
    """enable_thinking 是供应商特有参数,对不支持的供应商下发可能直接 400。"""
    c = _client(monkeypatch, OPENAI, "gpt-4o-mini", "auto")
    assert c.should_disable_thinking() is False


def test_on_keeps_provider_default(monkeypatch):
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "on")
    assert c.should_disable_thinking() is False


def test_off_applies_even_to_unknown_provider(monkeypatch):
    c = _client(monkeypatch, OPENAI, "gpt-4o-mini", "off")
    assert c.should_disable_thinking() is True


def test_empty_value_falls_back_to_auto(monkeypatch):
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "")
    assert c.should_disable_thinking() is True


# ---------- 真正发出去的请求 ----------

def test_chat_sends_enable_thinking_false(monkeypatch):
    sink: dict = {}
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "auto")
    monkeypatch.setattr(c, "_client", lambda: _fake_client(sink))
    c.chat([{"role": "user", "content": "hi"}])
    assert sink.get("extra_body") == {"enable_thinking": False}


def test_chat_omits_param_for_other_provider(monkeypatch):
    sink: dict = {}
    c = _client(monkeypatch, OPENAI, "gpt-4o-mini", "auto")
    monkeypatch.setattr(c, "_client", lambda: _fake_client(sink))
    c.chat([{"role": "user", "content": "hi"}])
    assert "extra_body" not in sink


def test_explicit_call_argument_wins(monkeypatch):
    """调用方显式指定时要尊重它(便于单次实验对比)。"""
    sink: dict = {}
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "auto")
    monkeypatch.setattr(c, "_client", lambda: _fake_client(sink))
    c.chat([{"role": "user", "content": "hi"}], extra_body={"enable_thinking": True})
    assert sink["extra_body"] == {"enable_thinking": True}


def test_generate_goes_through_the_same_path(monkeypatch):
    """生成与提取张力/论点都走 chat(),所以要一起生效。"""
    sink: dict = {}
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", "auto")
    monkeypatch.setattr(c, "_client", lambda: _fake_client(sink))
    c.generate("sys", "user")
    assert sink["extra_body"] == {"enable_thinking": False}
    assert sink["messages"][0]["role"] == "system"


@pytest.mark.parametrize("mode,expected", [
    ("auto", True), ("on", False), ("off", True), ("on ", False), ("OFF", True),
])
def test_mode_parsing_is_case_and_space_tolerant(monkeypatch, mode, expected):
    c = _client(monkeypatch, DASHSCOPE, "qwen3.6-plus", mode)
    assert c.should_disable_thinking() is expected
