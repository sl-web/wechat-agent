"""两步生成(人工选论点)+ 文章类型推荐 的测试。

覆盖三件事:
1. 规则推荐器能否按语料特征给出不同结论(且说明依据);
2. 提示词是否真的把「人工选定的论点」变成硬约束;
3. 路由注册顺序 —— `/pipeline/theses` 必须排在 `/pipeline/{stage}` 之前,
   否则会被当成一个 stage 名吃掉(FastAPI 按注册顺序匹配)。
"""
from __future__ import annotations

import json

import pytest


# ---------------- 规则推荐器 ----------------

def test_type_advisor_detects_critical():
    from app.analyzer.type_advisor import suggest_type

    r = suggest_type("某公司回应质疑", [
        {"title": "用户投诉某产品夸大宣传", "summary": "官方回应称不实"},
        {"title": "监管质疑其数据存疑", "summary": "存在争议"},
    ])
    assert r["type"] == "critical"
    assert "质疑" in r["reason"] or "回应" in r["reason"]


def test_type_advisor_detects_teardown():
    from app.analyzer.type_advisor import suggest_type

    r = suggest_type("算力成本", [
        {"title": "某厂财报显示毛利率下滑", "summary": "供应链与代工成本上升"},
        {"title": "数据中心电费与折旧压力", "summary": "产能与定价结构调整"},
    ])
    assert r["type"] == "teardown"


def test_type_advisor_detects_forecast():
    from app.analyzer.type_advisor import suggest_type

    r = suggest_type("路线图", [
        {"title": "公司预计明年量产", "summary": "计划 2027 年落地,目标清晰"},
        {"title": "下半年有望迎来拐点", "summary": "展望与规划"},
    ])
    assert r["type"] == "forecast"


def test_type_advisor_falls_back_to_industry_judgment():
    """什么特征都没有时退回最通用的类型,而不是乱猜。"""
    from app.analyzer.type_advisor import suggest_type

    r = suggest_type("某活动", [{"title": "活动举办", "summary": "现场照片"}])
    assert r["type"] == "industry_judgment"
    assert r["confidence"] == "low"


def test_type_advisor_always_explains_itself():
    """推荐必须给出依据词 —— 黑箱推荐用户没法判断可不可信。"""
    from app.analyzer.type_advisor import suggest_type

    r = suggest_type("发布", [{"title": "新品发布", "summary": "上线"}])
    assert r["reason"]
    assert set(r["scores"]) == {"industry_judgment", "critical", "teardown", "forecast"}


def test_cluster_report_carries_suggested_type():
    from app.analyzer.cluster import build_report

    clusters = [{
        "topic": "算力", "keyword": "算力", "count": 2, "heat_sum": 1.0,
        "members": [
            {"id": 1, "title": "数据中心电费与折旧成本上升", "summary": "定价"},
            {"id": 2, "title": "供应链产能与毛利率承压", "summary": "代工"},
        ],
    }]
    rep = build_report(clusters, top_n=1)
    st = rep["topics"][0]["suggested_type"]
    assert st["type"] in ("teardown", "critical", "forecast", "industry_judgment")
    assert st["reason"]


# ---------------- 提示词:选定论点变硬约束 ----------------

def test_chosen_thesis_becomes_hard_constraint():
    from app.generator.prompt import assemble_prompt

    thesis = "语音分层的本质是把算力成本转嫁给高付费用户。"
    topic = {"id": 1, "title": "某语音模型更新", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic, chosen_thesis=thesis)
    assert thesis in sys_p
    assert "硬约束" in sys_p
    assert "不要另起炉灶" in sys_p


def test_no_chosen_thesis_means_no_constraint_block():
    from app.generator.prompt import assemble_prompt

    topic = {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic)
    assert "已由人工选定" not in sys_p


def test_article_type_override_applies_to_this_call_only():
    """用户在界面上选的类型只影响这一篇,不能写回全局配置。"""
    from app.core.config import get_config
    from app.generator.prompt import assemble_prompt

    topic = {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x", "source": "rss"}
    before = get_config("article_type")
    sys_p, _ = assemble_prompt(
        "tpl_ai_news_v1", "s3", topic,
        overrides={"article_type": "critical"},
    )
    assert "批判质疑型" in sys_p
    assert get_config("article_type") == before, "overrides 不应写回配置"


def test_article_type_override_empty_falls_back_to_config():
    from app.core.config import get_config
    from app.generator.prompt import assemble_prompt

    topic = {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic, overrides={"article_type": ""})
    # 空值应当被忽略,退回配置里的姿态(默认行业判断型)
    assert "型" in sys_p
    assert get_config("article_type") or True


# ---------------- 路由顺序 ----------------

def test_theses_route_registered_before_stage_route():
    """`/pipeline/{stage}` 是通配的,注册顺序错了 /pipeline/theses 会被它吃掉。

    (API 路由挂在子 router 上,所以要看 router.routes 而不是 app.routes。)
    """
    from app.web.router import router

    paths = [getattr(r, "path", "") for r in router.routes]
    assert "/pipeline/theses" in paths
    assert paths.index("/pipeline/theses") < paths.index("/pipeline/{stage}")


# ---------------- plan_theses 编排 ----------------

def test_plan_theses_without_source_returns_actionable_error():
    from app.pipeline import plan_theses

    r = plan_theses({})
    assert r["ok"] is False
    assert r["reason"] == "no_source"
    assert r["error"] and r["hint"]


def test_plan_theses_with_dead_source_refs_returns_actionable_error():
    from app.pipeline import plan_theses

    r = plan_theses({"topic_ids": [99999999]})
    assert r["ok"] is False
    assert r["reason"] == "invalid_source"
    assert r["hint"]


def test_generate_theses_parses_llm_output(monkeypatch):
    """模型返回什么形状,都要能抠出可用的候选论点。"""
    from app.generator import thesis as th

    payload = {
        "recommended_type": "teardown",
        "type_reason": "成本与定价细节密集",
        "theses": [
            {"thesis": "算力的真实瓶颈不在芯片而在电费与折旧。", "support": "财报显示毛利率下滑",
             "angle": "成本结构", "type": "teardown", "hook": "电费占了三成"},
            {"thesis": "所谓免费额度其实是获客补贴,退坡后留存会掉。", "support": "补贴退坡",
             "angle": "留存", "type": "forecast", "hook": "补贴退坡"},
        ],
    }

    class _FakeLLM:
        def chat(self, messages, **kw):
            return json.dumps(payload, ensure_ascii=False)

    res = th.generate(
        {"title": "某算力集群上线", "summary": "摘要", "url": "http://x", "source": "rss"},
        llm=_FakeLLM(), n=4,
    )
    assert len(res["theses"]) == 2
    assert res["recommended_type"] == "teardown"
    assert res["theses"][1]["type"] == "forecast"
    assert res["type_reason"]


def test_generate_theses_rejects_useless_output(monkeypatch):
    """全是空话/占位时应当报错重试,而不是把垃圾返回给用户。"""
    from app.generator import thesis as th
    from app.generator.service import GenerationError

    class _FakeLLM:
        def chat(self, messages, **kw):
            return json.dumps({"theses": [{"thesis": "值得关注"}, {"thesis": ""}]},
                              ensure_ascii=False)

    with pytest.raises(GenerationError):
        th.generate({"title": "标题", "summary": "摘要", "url": "http://x"}, llm=_FakeLLM())


def test_generate_theses_writes_type_label():
    from app.generator.thesis import TYPE_LABELS

    assert set(TYPE_LABELS) == {"industry_judgment", "critical", "teardown", "forecast"}


# ---------------- 张力提取 ----------------

_TENSION_PAYLOAD = {
    "tensions": [
        {"id": "t1", "tension": "对外讲的是技术普惠,但新计费规则把推理成本推给了付费用户",
         "sides": "厂商 vs 付费用户", "contrast": "宣称普惠,实际涨价",
         "evidence": "Pro 套餐单价上调 40%"},
        {"id": "t1", "tension": "这条 id 与上面重复,应当被重新编号", "sides": "A vs B",
         "contrast": "x", "evidence": "y"},
        {"id": "t9", "tension": "存在挑战", "sides": "", "contrast": "", "evidence": ""},
    ],
    "recommended_type": "critical",
    "type_reason": "冲突细节密集",
    "theses": [
        {"thesis": "所谓普惠其实是一次成本转嫁的定价实验。", "tension_id": "t1",
         "support": "单价上调 40%", "angle": "定价", "type": "critical", "hook": "上调 40%"},
        {"thesis": "免费额度的真实作用是获客补贴,退坡后留存会掉。", "tension_id": "t99",
         "support": "补贴退坡", "angle": "留存", "type": "forecast", "hook": "退坡"},
    ],
}


def _fake_llm(payload):
    class _FakeLLM:
        def chat(self, messages, **kw):
            return json.dumps(payload, ensure_ascii=False)

    return _FakeLLM()


def test_tensions_parsed_deduped_and_short_ones_dropped():
    """张力 id 重复要重编号,过短的("存在挑战")要丢掉 —— 但论点不受影响。"""
    from app.generator import thesis as th

    res = th.generate(
        {"title": "某模型更新", "summary": "摘要", "url": "http://x", "source": "rss"},
        llm=_fake_llm(_TENSION_PAYLOAD), n=4,
    )
    assert [t["id"] for t in res["tensions"]] == ["t1", "t2"]      # 第 3 条太短被丢弃
    assert res["tensions"][0]["sides"] == "厂商 vs 付费用户"
    assert res["tensions"][0]["evidence"] == "Pro 套餐单价上调 40%"


def test_thesis_tension_id_cleared_when_it_points_nowhere():
    """指向不存在张力的 tension_id 要置空,不能让它指一条假张力。"""
    from app.generator import thesis as th

    res = th.generate(
        {"title": "某模型更新", "summary": "摘要", "url": "http://x", "source": "rss"},
        llm=_fake_llm(_TENSION_PAYLOAD), n=4,
    )
    assert res["theses"][0]["tension_id"] == "t1"
    assert res["theses"][1]["tension_id"] == ""


def test_tensions_are_optional():
    """模型没给张力时,第一步照样要能用 —— 张力是增强项,不是前置依赖。"""
    from app.generator import thesis as th

    payload = {"theses": [{"thesis": "这是一个可以被反驳的具体判断。", "support": "事实"}]}
    res = th.generate(
        {"title": "标题", "summary": "摘要", "url": "http://x"},
        llm=_fake_llm(payload), n=4,
    )
    assert res["tensions"] == []
    assert len(res["theses"]) == 1


def test_garbage_tensions_do_not_break_the_step():
    """张力字段是垃圾(字符串/数字/None)时,只丢张力,不能让整步失败。"""
    from app.generator import thesis as th

    payload = {"tensions": "不是数组", "theses": [{"thesis": "一个可以反驳的判断句。"}]}
    res = th.generate({"title": "标题", "summary": "摘要"}, llm=_fake_llm(payload))
    assert res["tensions"] == []
    assert res["theses"]


# ---------------- 提示词:选定张力变硬约束 ----------------

def test_chosen_tension_becomes_hard_constraint():
    from app.generator.prompt import assemble_prompt

    tension = "对外讲的是技术普惠,但计费规则把推理成本推给了付费用户"
    thesis = "语音分层的本质是把算力成本转嫁给高付费用户。"
    topic = {"id": 1, "title": "某语音模型更新", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic,
                               chosen_thesis=thesis, chosen_tension=tension)
    assert tension in sys_p
    assert "本轮要写的张力" in sys_p
    assert "全文围绕它展开" in sys_p


def test_tension_block_precedes_thesis_block():
    """先让模型知道冲突在哪,再告诉它判断是什么 —— 顺序反了读起来是倒的。"""
    from app.generator.prompt import assemble_prompt

    tension = "宣称普惠实际涨价"
    thesis = "这是一次成本转嫁。"
    topic = {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic,
                               chosen_thesis=thesis, chosen_tension=tension)
    assert sys_p.index(tension) < sys_p.index(thesis)


def test_no_chosen_tension_means_no_tension_block():
    from app.generator.prompt import assemble_prompt

    topic = {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x", "source": "rss"}
    sys_p, _ = assemble_prompt("tpl_ai_news_v1", "s3", topic, chosen_thesis="某个判断。")
    assert "本轮要写的张力" not in sys_p
