"""配置「真能生效」的回归测试。

背景:控制面板上有 9 个配置项**代码里根本没人读**(审计发现),其中 6 个还是面板上暴露给用户的
—— 也就是说用户改了它,系统行为不会有任何变化。这个文件把已接通的几项钉死:

  retry_limit         → 熔断重试上限(以前写死 3;而且循环里还多写了一个 `>= 3` 的 break)
  anti_recap_guard    → 反综述护栏开关(以前从**默认库**读,带 db_path 跑时读错库)
  review_stale_hours  → 滞留告警阈值(以前路由写死 24)
  backfill_enabled    → 定时回流开关(以前没人读,关掉也照样跑)

⚠️ 这些测试**必须只依赖传入的临时库**,不能隐式读真实库(那正是它们要防的 bug)。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import set_config
from app.core.db import connect, init_db
from app.generator.llm import LLMClient
from app.generator.service import GenerationError, generate_article
from app.generator.templates import seed_default_template
from app.generator.validator import Validator


def _prepare_db(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    seed_default_template(str(db))
    conn = connect(db)
    tid = conn.execute(
        """INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date)
           VALUES('rss','rss','主选题标题','http://x','2026-09-10')"""
    ).lastrowid
    conn.commit()
    conn.close()
    return db, tid


def _put(db, key, value):
    conn = connect(db)
    set_config(key, value, db=conn)
    conn.close()


TOPIC = {"id": 1, "title": "主选题标题", "url": "http://x", "source": "rss", "summary": "摘要"}


# ---------------- retry_limit ----------------

class _AlwaysBadLLM(LLMClient):
    """永远产出不合格内容(太短),用来数"到底重试了几次"。"""

    def __init__(self):
        self.calls = 0

    def generate(self, system_prompt, user_prompt, **kw):
        self.calls += 1
        return json.dumps({"title": "短标题", "lead": "导语", "body": "太短",
                           "quote": "q", "cta": "c"}, ensure_ascii=False)


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2), (4, 4)])
def test_retry_limit_from_config_is_honored(tmp_path, limit, expected):
    """重试上限唯一来源是配置 `retry_limit`。

    以前:CB 写死 3,循环里**还有**一句 `if fail_count >= 3: break` ——
    调大配置完全没用(这个参数化用例正是为它准备的:limit=4 时旧代码只会调 3 次)。
    """
    db, tid = _prepare_db(tmp_path)
    _put(db, "retry_limit", limit)

    llm = _AlwaysBadLLM()
    with pytest.raises(GenerationError):
        generate_article({**TOPIC, "id": tid}, "tpl_ai_news_v1", "s1", "run-rl",
                         llm=llm, db_path=str(db))
    assert llm.calls == expected


def test_retry_limit_falls_back_when_garbage(tmp_path):
    """配置被写成垃圾值时回落到 3,不能直接崩。"""
    db, tid = _prepare_db(tmp_path)
    _put(db, "retry_limit", "不是数字")

    llm = _AlwaysBadLLM()
    with pytest.raises(GenerationError):
        generate_article({**TOPIC, "id": tid}, "tpl_ai_news_v1", "s1", "run-rl2",
                         llm=llm, db_path=str(db))
    assert llm.calls == 3


# ---------------- anti_recap_guard ----------------

RECAP = {"title": "关于 AI 的一些观察", "lead": "近年来人工智能发展迅速,值得关注。",
         "body": "近年来人工智能发展迅速,影响深远,值得关注。各行各业都在积极拥抱这一趋势。",
         "quote": "q", "cta": "c"}


def test_anti_recap_guard_reads_the_same_db(tmp_path):
    """护栏开关必须从**传进来的那个库**读。

    旧实现 `get_config("anti_recap_guard", None)` 读的是默认库 ——
    模板来自 A 库、开关来自 B 库。这里把临时库设成 False:
    若实现读错库(真实库默认 True),"综述腔"这条就会冒出来,断言失败。
    """
    db, _ = _prepare_db(tmp_path)

    _put(db, "anti_recap_guard", False)
    off = Validator("tpl_ai_news_v1", "s3", db_path=str(db)).validate(dict(RECAP))["issues"]
    assert not any("综述腔" in i for i in off), "关掉护栏后不该再判综述腔"

    _put(db, "anti_recap_guard", True)
    on = Validator("tpl_ai_news_v1", "s3", db_path=str(db)).validate(dict(RECAP))["issues"]
    assert any("综述腔" in i for i in on), "开启护栏时综述腔必须被判出来"


# ---------------- review_stale_hours ----------------

def test_stale_route_defaults_to_configured_hours(monkeypatch):
    """`/dashboard/stale` 不传参时用配置阈值;显式传参仍可覆盖。

    只在**接线层**验证(打桩 get_config 与 review_stale),不读真实库。
    """
    from app.web import router as R

    monkeypatch.setattr("app.core.config.get_config", lambda key, db=None: 6)
    seen: dict = {}
    monkeypatch.setattr(R, "review_stale", lambda stale_hours=24: seen.update(h=stale_hours) or [])

    R.api_stale()
    assert seen["h"] == 6, "默认阈值应来自配置 review_stale_hours"

    R.api_stale(hours=99)
    assert seen["h"] == 99, "显式传参应仍然生效"


def test_stale_route_falls_back_when_config_broken(monkeypatch):
    from app.web import router as R

    monkeypatch.setattr("app.core.config.get_config", lambda key, db=None: "垃圾")
    seen: dict = {}
    monkeypatch.setattr(R, "review_stale", lambda stale_hours=24: seen.update(h=stale_hours) or [])
    R.api_stale()
    assert seen["h"] == 24


# ---------------- backfill_enabled ----------------

def test_backfill_disabled_skips_scheduled_run(monkeypatch):
    """关掉「效果回流开关」后,定时回流不该真的去抓后台。"""
    from app import pipeline as P

    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("关闭时不该调用 refresh_all")

    monkeypatch.setattr("app.core.config.get_config", lambda key, db=None: False)
    monkeypatch.setattr(P, "refresh_all", _boom)

    out = P.run_backfill()
    assert out.get("disabled") is True
    assert called["n"] == 0


def test_backfill_enabled_still_runs(monkeypatch):
    """开着的时候照旧执行(别为了加开关把主流程弄坏)。"""
    from app import pipeline as P

    monkeypatch.setattr("app.core.config.get_config", lambda key, db=None: True)
    monkeypatch.setattr(P, "refresh_all", lambda **kw: {
        "sync": {"matched": 2}, "stats": {"checked": 3, "backfilled": 3,
                                          "skipped": [], "failed": []}})

    out = P.run_backfill()
    assert out["backfilled"] == 3
    assert out["sync_matched"] == 2
    assert not out.get("disabled")
