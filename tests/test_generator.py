"""生成器测试:模板管理、提示词事实约束、校验器、编排熔断。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.db import connect, init_db, query
from app.generator.llm import LLMClient
from app.generator.prompt import assemble_prompt, build_user_prompt
from app.generator.service import _parse_llm_output, generate_article
from app.generator.templates import (
    get_template,
    save_template,
    seed_default_template,
)
from app.generator.validator import Validator


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


def test_seed_default_template(db_path):
    tid = seed_default_template(str(db_path))
    assert tid == "tpl_ai_news_v1"
    tpl = get_template(tid, str(db_path))
    assert "structures" in tpl["definition"]


def test_save_and_get_template(db_path):
    tpl = {
        "template_id": "tpl_test",
        "name": "测试模板",
        "structures": [{"structure_id": "s1", "name": "结构A",
                        "sections": [{"key": "title", "desc": "标题"},
                                     {"key": "body", "desc": "正文 100~200 字"}]}],
        "style": {"tone": "专业"},
    }
    save_template(tpl, db_path=str(db_path))
    loaded = get_template("tpl_test", str(db_path))
    assert loaded["name"] == "测试模板"
    assert loaded["definition"]["structures"][0]["structure_id"] == "s1"


def test_prompt_contains_fact_constraints(db_path):
    seed_default_template(str(db_path))
    sys_p, usr_p = assemble_prompt(
        "tpl_ai_news_v1", "s1",
        {"title": "某公司发布新模型", "summary": "参数规模提升", "url": "http://x", "source": "wechat"},
        db_path=str(db_path),
    )
    assert "禁止编造" in sys_p
    assert "【待核实】" in sys_p
    assert "信息来源" in sys_p
    assert "某公司发布新模型" in usr_p


def test_validator_ok(db_path):
    seed_default_template(str(db_path))
    v = Validator("tpl_ai_news_v1", "s1", db_path=str(db_path))
    # 结构齐全、字数在 1200~1800 区间
    result = {
        "title": "大模型刷屏背后",
        "lead": "导语" * 50,
        "body": "据公开报道,事件回顾正文" * 60,  # ~400 字,不够
        "quote": "金句",
        "cta": "关注转发",
    }
    r = v.validate(result)
    assert r["ok"] is False  # 字数不足
    assert any("字数" in i for i in r["issues"])


def test_validator_sensitive_word(db_path):
    seed_default_template(str(db_path))
    v = Validator("tpl_ai_news_v1", "s1", ban_words=["绝对"], db_path=str(db_path))
    body = "这绝对是本年度最佳"
    r = v.validate({"title": "t", "lead": "l", "body": body, "quote": "q", "cta": "c"})
    assert any("违禁词" in i for i in r["issues"])


# ---------- 违禁词的两个真问题(实测踩过) ----------

def _ban_body(text: str) -> dict:
    return {"title": "t", "lead": "l", "body": f"据公开报道,{text}。" * 40,
            "quote": "q", "cta": "c"}


def test_ban_exempt_allows_legit_compounds(db_path):
    """「最优解/最佳实践」是术语与通用表达,不能按广告法极限词判死。

    真实故障:一篇正常分析因为写了「最优解」被判"命中违禁词: ['最优']",
    触发**整篇重写**(每次 150 秒以上),用户看到的现象是"生成卡住不动"。
    """
    seed_default_template(str(db_path))
    v = Validator("tpl_ai_news_v1", "s1", db_path=str(db_path))
    r = v.validate(_ban_body("这不是最优解,而是行业里的最佳实践"))
    assert not any("违禁词" in i for i in r["issues"]), r["issues"]


def test_ban_exempt_still_catches_superlative_claim(db_path):
    """豁免不能把口子开太大:「最优的选择」这种极限表述仍要拦。"""
    seed_default_template(str(db_path))
    v = Validator("tpl_ai_news_v1", "s1", db_path=str(db_path))
    r = v.validate(_ban_body("这是最优的选择"))
    assert any("违禁词" in i for i in r["issues"])


def test_prompt_and_validator_use_same_ban_list(db_path):
    """prompt 的红线提示与 validator 的判定必须**同一张表**。

    以前 prompt 读模板里的 style.ban_words、validator 用 BAN_WORDS_DEFAULT →
    模型被要求避开 A、却被按 B 判罚,怎么改都可能不过。
    """
    from app.generator.validator import resolve_ban_words

    seed_default_template(str(db_path))
    tpl = get_template("tpl_ai_news_v1", db_path=str(db_path))
    words = resolve_ban_words(tpl)
    sys_p, _ = assemble_prompt(
        "tpl_ai_news_v1", "s1",
        {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x"},
        db_path=str(db_path),
    )
    assert all(w in sys_p for w in words)
    # validator 不显式传 ban_words 时,用的也必须是这张表
    v = Validator("tpl_ai_news_v1", "s1", db_path=str(db_path))
    assert v.ban_words == words


def test_stale_template_ban_words_is_synced(db_path):
    """老库带着早期的单字表 ['绝对','最','第一'] 时,同步要能纠正过来。"""
    from app.generator.templates import sync_default_ban_words

    seed_default_template(str(db_path))
    conn = connect(db_path)
    tpl = get_template("tpl_ai_news_v1", db_path=str(db_path))
    tpl["definition"]["style"]["ban_words"] = ["绝对", "最", "第一"]   # 模拟历史遗留
    conn.execute("UPDATE templates SET definition=? WHERE id=?",
                 (json.dumps(tpl["definition"], ensure_ascii=False), "tpl_ai_news_v1"))
    conn.commit()
    conn.close()

    assert sync_default_ban_words(str(db_path)) is True
    fixed = get_template("tpl_ai_news_v1", db_path=str(db_path))
    assert "最" not in fixed["definition"]["style"]["ban_words"]
    assert "最优" in fixed["definition"]["style"]["ban_words"]
    # 幂等:再同步一次不应有改动
    assert sync_default_ban_words(str(db_path)) is False


def test_retry_issues_are_injected_into_prompt(db_path):
    """重试要**靶向改**:把上一稿的问题回灌给模型,而不是盲重写一遍。"""
    seed_default_template(str(db_path))
    sys_p, _ = assemble_prompt(
        "tpl_ai_news_v1", "s1",
        {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x"},
        db_path=str(db_path),
        retry_issues=["命中违禁词: ['最优']"],
    )
    assert "命中违禁词: ['最优']" in sys_p
    assert "针对性改掉" in sys_p
    assert "论点、张力、结构都保持不变" in sys_p


def test_no_retry_issues_means_no_block(db_path):
    seed_default_template(str(db_path))
    sys_p, _ = assemble_prompt(
        "tpl_ai_news_v1", "s1",
        {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x"},
        db_path=str(db_path),
    )
    assert "针对性改掉" not in sys_p


def test_generate_article_with_mock_llm(db_path):
    """mock LLM 返回合法 JSON → 成功入库。"""
    seed_default_template(str(db_path))

    # 先建热点记录以满足外键
    conn = connect(db_path)
    topic_id = conn.execute(
        "INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date) VALUES('wechat','crawler','某热点','http://x','2026-09-09')"
    ).lastrowid
    conn.commit()
    conn.close()

    class MockLLM(LLMClient):
        def __init__(self):
            pass

        def generate(self, system_prompt, user_prompt, **kw):
            return json.dumps({
                "title": "测试文章标题",
                "lead": "导语" * 60,
                "body": "据公开报道,正文内容再补充一些细节叙述展开细节与影响分析" * 35,  # ~1300 字
                "quote": "引用金句",
                "cta": "关注我们" * 20,
            }, ensure_ascii=False)

    topic = {"id": topic_id, "title": "某热点", "summary": "概述", "url": "http://x"}
    result = generate_article(
        topic, "tpl_ai_news_v1", "s1", "run_test_1",
        llm=MockLLM(), db_path=str(db_path),
    )
    assert result["ok"] is True
    assert result["id"] > 0
    conn = connect(db_path)
    art = query(conn, "SELECT * FROM articles WHERE id=?", (result["id"],))[0]
    conn.close()
    assert art["status"] == "generated"


def test_generate_article_fallback_on_repeated_failure(db_path):
    """LLM 持续返回非法 JSON → 达到熔断上限后生成失败落库。"""
    seed_default_template(str(db_path))

    conn = connect(db_path)
    topic_id = conn.execute(
        "INSERT INTO hot_topics(source, fetch_mode, title, url, fetch_date) VALUES('wechat','crawler','坏热点','','2026-09-09')"
    ).lastrowid
    conn.commit()
    conn.close()

    class BadLLM(LLMClient):
        def __init__(self):
            pass

        def generate(self, system_prompt, user_prompt, **kw):
            return "这不是JSON"

    topic = {"id": topic_id, "title": "坏热点", "summary": "", "url": ""}
    with pytest.raises(Exception):
        generate_article(
            topic, "tpl_ai_news_v1", "s1", "run_test_2",
            llm=BadLLM(), db_path=str(db_path),
        )
    conn = connect(db_path)
    rows = query(conn, "SELECT * FROM articles WHERE topic_id=?", (topic_id,))
    conn.close()
    # 生成失败记录落库
    assert any(r["status"] == "generation_failed" for r in rows)


# ---- LLM 输出解析容错 ----
def test_parse_plain_json():
    r = _parse_llm_output('{"title": "T", "body": "B"}')
    assert r["title"] == "T" and r["body"] == "B"


def test_parse_json_in_codeblock():
    raw = '```json\n{"title": "T2", "lead": "L"}\n```'
    assert _parse_llm_output(raw)["title"] == "T2"


def test_parse_json_embedded_in_text():
    raw = '好的,以下是文章:\n{"title": "T3", "body": "内容"}\n希望对你有帮助'
    assert _parse_llm_output(raw)["title"] == "T3"


def test_parse_markdown_style_output():
    """真实 qwen 曾返回 Markdown 排版而非 JSON,须容错解析。"""
    raw = """**候选标题**
1. 腾讯混元限免将收官
2. 混元 Hy4 权益调整

**导语** 近日腾讯宣布调整权益,本文梳理脉络。

**正文** 一、事件回顾:据公开报道… 二、影响分析… 三、展望… 内容较长,分多行。
**金句** 这是一次重要调整。
**结尾** 关注我们了解更多。"""
    r = _parse_llm_output(raw)
    assert "腾讯混元限免将收官" in r.get("title", "")
    assert "权益" in r.get("lead", "")
    assert "事件回顾" in r.get("body", "")
    assert "关注我们" in r.get("cta", "")


def test_parse_unparseable_raises():
    from app.generator.service import GenerationError

    try:
        _parse_llm_output("完全无关的文本,没有结构")
        assert False, "应当抛 GenerationError"
    except GenerationError:
        pass
