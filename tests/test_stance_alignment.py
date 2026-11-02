"""论证姿态必须与「控制面板」的配置对齐。

回归背景(用户实测报的):
控制面板把 `article_type` 设成「行业判断型」,但「生成候选论点」出来的论点、
以及据此写出的文章,全是「批判质疑型」的口吻 —— 配置形同虚设。

三处原因(全部修掉,每条都在下面钉死):
1. 候选论点提示词**根本没读** `article_type`,反而告诉模型"按素材自己挑类型",
   而判断规则里写着"素材里矛盾冲突多 → 更适合批判质疑型" → 必然挑成批判;
2. 前端把模型的 `recommended_type` 当默认值,并作为 override 传给生成阶段,
   于是"配置"被一个建议值覆盖;
3. 写作提示词里"必须写出主流看法并说明你为什么不同意"原本是**所有姿态的通用要求**,
   等于把行业判断型也逼成批判稿。
"""
from __future__ import annotations

from app.generator.prompt import ARGUMENT_MODES, build_system_prompt
from app.generator.thesis import TYPE_LABELS, build_prompt

TOPIC = {"id": 1, "title": "某算力集群上线", "summary": "摘要", "url": "http://x"}


def _cfg(article_type: str, **extra) -> dict:
    return {"writer_persona": "科技观察者", "writer_stance": "先问谁付钱",
            "writer_lens": "看成本结构", "article_type": article_type, **extra}


# ---------------- 第一步:候选论点 ----------------

def test_thesis_prompt_carries_configured_argument_mode():
    """配置的论证姿态**必须**进提示词(以前一个字都没进去)。"""
    sysp, _ = build_prompt(TOPIC, "素材若干", 4, cfg=_cfg("industry_judgment"))
    assert ARGUMENT_MODES["industry_judgment"] in sysp


def test_thesis_prompt_follows_whichever_mode_is_configured():
    """换配置就换提示词 —— 不是写死了行业判断型。"""
    for mode in ("critical", "teardown", "forecast"):
        sysp, _ = build_prompt(TOPIC, "素材若干", 4, cfg=_cfg(mode))
        assert ARGUMENT_MODES[mode] in sysp, f"{mode} 的姿态指引没进提示词"
        assert f"「{TYPE_LABELS[mode]}」" in sysp


def test_thesis_prompt_no_longer_lets_model_pick_type_freely():
    """不能再有"按素材自己挑类型"的自由发挥指令 —— 那正是配置被顶掉的原因。"""
    sysp, _ = build_prompt(TOPIC, "素材若干", 4, cfg=_cfg("industry_judgment"))
    assert "素材里矛盾冲突多 → 更适合批判质疑型" not in sysp
    # recommended_type 默认必须是配置值
    assert '默认填 industry_judgment' in sysp


def test_thesis_prompt_defaults_when_config_missing():
    """没配过(空值)时回落到行业判断型,不能报错。"""
    sysp, _ = build_prompt(TOPIC, "素材若干", 4, cfg=_cfg(""))
    assert ARGUMENT_MODES["industry_judgment"] in sysp


# ---------------- 第二步:写作(论点要求随姿态变化) ----------------

def _sys_prompt(mode: str) -> str:
    # 复用生产入口:`overrides` 就是两步生成里从界面传下来的那一次选择。
    # 结构必须标 `must_take_stance`(真实模板的 s3 就是),否则「论点要求」块不会出现。
    template = {"definition": {
        "style": {},
        "structures": [{"structure_id": "s3", "name": "论点驱动式",
                        "must_take_stance": True,
                        "sections": [{"key": "title", "desc": "标题"}]}],
    }}
    return build_system_prompt(template, "s3", overrides={"article_type": mode})


def test_writing_prompt_critical_asks_for_counter_argument():
    p = _sys_prompt("critical")
    assert "说明你为什么不同意" in p


def test_writing_prompt_industry_judgment_is_not_forced_into_critique():
    """⚠️ 行业判断型不该被要求"站到主流的对立面" —— 这是把配置顶掉的第三处原因。"""
    p = _sys_prompt("industry_judgment")
    assert "说明你为什么不同意" not in p
    assert "先写出主流预期或通行说法,再给出你的判断" in p
    assert ARGUMENT_MODES["industry_judgment"] in p


def test_writing_prompt_forecast_also_not_forced_into_critique():
    p = _sys_prompt("forecast")
    assert "说明你为什么不同意" not in p
    assert ARGUMENT_MODES["forecast"] in p


def test_writing_prompt_teardown_talks_about_mechanism():
    p = _sys_prompt("teardown")
    assert "漏掉了哪一环" in p
    assert ARGUMENT_MODES["teardown"] in p
