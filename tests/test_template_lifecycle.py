"""模板生命周期:人工设计的模板要「存得住、真的被用上」。

背景(全是实测踩到的,别再改回去):
1. 用户在「控制面板 → 模板库」改好模板、点了保存(库里 updated_at 确实变了),
   可**一生成就变回原样** —— `pipeline.py` 每次生成前都会调 `seed_default_template()`,
   而旧实现拿代码里的默认 sections 覆盖同名结构,等于把用户的改动回滚掉。
2. 「生成时用哪个模板」以前只被 pipeline 硬编码读 `cfg.get("default_template", ...)`,
   `DEFAULTS` 里没有、面板上也没有这个开关 → 新建的模板**永远选不上**,
   用户看到的就是"模板保存了却没作用"。
3. s2 盘点式的正文描述写的是「每条 150~250 字」,被 validator 当成**全文**字数区间
   (允许窗口 120~300)→ 1500 字的正文永远"字数不达标",模型还照着这个数字越改越短
   (实测 1552 → 1267 → 914 字),最后熔断。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import DEFAULTS
from app.core.db import connect, init_db
from app.generator.prompt import assemble_prompt, build_share_hooks
from app.generator.templates import (
    DEFAULT_TEMPLATE_ID,
    get_template,
    list_templates,
    resolve_structure,
    save_template,
    seed_default_template,
    toggle_template,
)
from app.generator.validator import Validator

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_path(tmp_path: Path):
    p = tmp_path / "test.db"
    init_db(p)
    return p


# ---------------------------------------------------------------- 工具
def _body_section(db_path, structure_id: str, template_id: str = DEFAULT_TEMPLATE_ID) -> dict:
    tpl = get_template(template_id, db_path=str(db_path))
    struct = next(s for s in tpl["definition"]["structures"] if s["structure_id"] == structure_id)
    return next(x for x in struct["sections"] if x["key"] == "body")


def _set_definition(db_path, definition: dict, template_id: str = DEFAULT_TEMPLATE_ID) -> None:
    conn = connect(db_path)
    conn.execute("UPDATE templates SET definition=? WHERE id=?",
                 (json.dumps(definition, ensure_ascii=False), template_id))
    conn.commit()
    conn.close()


def _save_custom(db_path, desc: str, extra: dict | None = None, template_id: str = "tpl_custom_t"):
    section = {"key": "body", "desc": desc}
    section.update(extra or {})
    save_template({
        "template_id": template_id, "name": "自定义",
        "structures": [{"structure_id": "s1", "name": "x", "sections": [
            {"key": "title", "desc": "标题"}, section, {"key": "cta", "desc": "结尾"},
        ]}],
    }, db_path=str(db_path))
    return Validator(template_id, "s1", db_path=str(db_path))


# ---------------------------------------------------------------- 字数区间
def test_explicit_min_max_wins_over_desc(db_path):
    """有显式 min_len/max_len 时以它为准 —— 描述里的数字可能说的是别的东西。"""
    v = _save_custom(db_path, "正文按条目盘点,每条 150~250 字", {"min_len": 1200, "max_len": 1800})
    assert v._guess_len_range() == (1200, 1800)


def test_per_item_range_is_not_treated_as_whole_article(db_path):
    """「每条 150~250 字」是单条长度,不能当全文长度(旧 bug 的根因)。"""
    v = _save_custom(db_path, "正文按条目盘点,每条 150~250 字。段落 60~150 字")
    assert v._guess_len_range() == (0, 0), "认不出全文长度就不该判字数,而不是拿 150~250 误杀"


def test_total_range_in_desc_is_parsed(db_path):
    v = _save_custom(db_path, "正文 1200~1800 字,分 3 小节;段落 60~150 字")
    assert v._guess_len_range() == (1200, 1800)


def test_no_numbers_means_no_length_check(db_path):
    assert _save_custom(db_path, "正文,分小节")._guess_len_range() == (0, 0)


def test_s2_accepts_a_normal_length_article(db_path):
    """回归:内置 s2 上一次把 1500 字的正文判成"字数不达标"。"""
    seed_default_template(str(db_path))
    v = Validator(DEFAULT_TEMPLATE_ID, "s2", db_path=str(db_path))
    assert v._guess_len_range() == (1200, 1800)
    r = v.validate({"title": "标题", "lead": "导语", "body": "具身智能的落地路径" * 120,
                    "cta": "结尾引导"})
    assert not any("字数不达标" in i for i in r["issues"]), r["issues"]


def test_prompt_tells_the_model_the_total_length(db_path):
    """描述里可能只写了"每条多少字",所以字数目标必须由 min_len/max_len 显式写进提示词。"""
    seed_default_template(str(db_path))
    sys_p, _ = assemble_prompt(
        DEFAULT_TEMPLATE_ID, "s2",
        {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x"},
        db_path=str(db_path),
    )
    assert "本字段总长度 1200~1800 字" in sys_p


# ---------------------------------------------------------------- 转发钩子按结构裁剪
def test_share_hooks_only_mention_fields_the_structure_has(db_path):
    """s2 没有 quote 字段,就不要再要求"quote 必须能单独转发" ——
    否则模型只能凭空造一个用不上的 quote,还把真正需要的 cta 写丢(实测 3 次丢 2 次)。"""
    seed_default_template(str(db_path))
    sys_p, _ = assemble_prompt(
        DEFAULT_TEMPLATE_ID, "s2",
        {"id": 1, "title": "标题", "summary": "摘要", "url": "http://x"},
        db_path=str(db_path),
    )
    assert "`cta`" in sys_p
    assert "`quote`" not in sys_p


def test_share_hooks_include_everything_when_fields_exist():
    block = build_share_hooks([{"key": "quote"}, {"key": "cta"}, {"key": "body"}])
    for key in ("`quote`", "`cta`", "`body`"):
        assert key in block
    assert build_share_hooks([]) == ""


# ---------------------------------------------------------------- seed 不再覆盖用户改动
def test_seed_keeps_user_edited_structure(db_path):
    """核心回归:用户改过的结构,生成前那次 seed 不能把它改回去。"""
    seed_default_template(str(db_path))
    tpl = get_template(DEFAULT_TEMPLATE_ID, db_path=str(db_path))
    d = tpl["definition"]
    body = next(x for x in next(s for s in d["structures"]
                                if s["structure_id"] == "s2")["sections"] if x["key"] == "body")
    user_text = "正文 900~1100 字(我自己定的),按条目盘点"
    body["desc"] = user_text
    save_template(d, db_path=str(db_path))          # 等价于"模板库点保存"

    seed_default_template(str(db_path))             # 等价于"生成前那次 seed"
    after = _body_section(db_path, "s2")
    assert after["desc"] == user_text, "用户手工改的结构被 seed 覆盖了"
    assert after.get("min_len") == 1200, "显式字数区间应随用户版本一起保留"


def test_seed_propagates_default_upgrade_only_when_untouched(db_path, monkeypatch):
    """升级默认模板时:用户没动过的结构会跟着更新,动过的一律保留。"""
    import app.generator.templates as T

    seed_default_template(str(db_path))
    tpl = get_template(DEFAULT_TEMPLATE_ID, db_path=str(db_path))
    d = tpl["definition"]
    body = next(x for x in next(s for s in d["structures"]
                                if s["structure_id"] == "s2")["sections"] if x["key"] == "body")
    user_text = "我自己的 s2 说明"
    body["desc"] = user_text
    save_template(d, db_path=str(db_path))

    new_default = json.loads(json.dumps(T.DEFAULT_TEMPLATE, ensure_ascii=False))
    for s in new_default["structures"]:
        if s["structure_id"] in ("s1", "s2"):
            next(x for x in s["sections"] if x["key"] == "body")["desc"] = "新版默认描述 2000~2400 字"
    monkeypatch.setattr(T, "DEFAULT_TEMPLATE", new_default)

    seed_default_template(str(db_path))
    assert _body_section(db_path, "s1")["desc"] == "新版默认描述 2000~2400 字", "没改过的结构应同步默认"
    assert _body_section(db_path, "s2")["desc"] == user_text, "改过的结构不该被默认顶掉"


def test_seed_backfills_length_fields_into_legacy_db(db_path):
    """老库(本次升级前入库的)只补字段,用户写过的文字一个字都不动。"""
    seed_default_template(str(db_path))
    tpl = get_template(DEFAULT_TEMPLATE_ID, db_path=str(db_path))
    d = tpl["definition"]
    d.pop("_seed_meta", None)                       # 模拟"没有标记的老库"
    s2_body = next(x for x in next(s for s in d["structures"]
                                   if s["structure_id"] == "s2")["sections"] if x["key"] == "body")
    legacy_text = "正文按条目盘点,每条 150~250 字"
    s2_body["desc"] = legacy_text
    for s in d["structures"]:
        for sec in s["sections"]:
            sec.pop("min_len", None)
            sec.pop("max_len", None)
    _set_definition(db_path, d)

    seed_default_template(str(db_path))
    after = _body_section(db_path, "s2")
    assert after["desc"] == legacy_text, "补字段不能改用户写的描述"
    assert (after.get("min_len"), after.get("max_len")) == (1200, 1800)
    # 描述里只有"每条 150~250"却有了显式区间 ⇒ 校验按 1200~1800 走,不再误杀
    assert Validator(DEFAULT_TEMPLATE_ID, "s2", db_path=str(db_path))._guess_len_range() == (1200, 1800)


def test_seed_is_idempotent(db_path):
    seed_default_template(str(db_path))
    first = get_template(DEFAULT_TEMPLATE_ID, db_path=str(db_path))["definition"]
    seed_default_template(str(db_path))
    assert get_template(DEFAULT_TEMPLATE_ID, db_path=str(db_path))["definition"] == first


# ---------------------------------------------------------------- 模板选用
def test_default_template_is_a_declared_config(db_path):
    """面板能改的键必须在 DEFAULTS 里声明,否则读写口径不一致。"""
    assert "default_template" in DEFAULTS
    assert DEFAULTS["default_template"] == DEFAULT_TEMPLATE_ID


def test_resolve_structure_falls_back_to_a_usable_one():
    tpl = {"definition": {"structures": [{"structure_id": "a"}, {"structure_id": "b"}]}}
    assert resolve_structure(tpl, "b") == "b"
    assert resolve_structure(tpl, "s3") == "a", "模板里没有这个结构就回落到第一个,而不是直接失败"
    assert resolve_structure(None, "s3") == "s3", "拿不到模板时原样返回,别把配置值也改掉"
    assert resolve_structure({"definition": {"structures": []}}, "") == "s1"


def test_custom_template_survives_seed_and_is_selectable(db_path):
    """新建一个模板 → 保存 → 再 seed(生成前会调)→ 内容必须还在,并且能被选为生成模板。"""
    seed_default_template(str(db_path))
    save_template({
        "template_id": "tpl_mine", "name": "我的模板",
        "structures": [{"structure_id": "s9", "name": "结构", "sections": [
            {"key": "title", "desc": "标题"}, {"key": "body", "desc": "正文", "min_len": 800, "max_len": 1000},
            {"key": "cta", "desc": "结尾"}]}],
    }, db_path=str(db_path))
    seed_default_template(str(db_path))
    tpl = get_template("tpl_mine", db_path=str(db_path))
    assert tpl is not None
    assert tpl["definition"]["name"] == "我的模板"
    assert resolve_structure(tpl, "s1") == "s9"     # 结构不属于它 → 回落
    assert resolve_structure(tpl, "s9") == "s9"


# ---------------------------------------------------------------- 前端契约(切标签不丢状态 / 模板能选)
def test_app_keeps_views_alive_across_tabs():
    """切标签只能隐藏视图,不能销毁 —— 否则用户这一页做的事全没了(实测反馈)。"""
    src = (ROOT / "wechat-agent-frontend" / "src" / "App.vue").read_text(encoding="utf-8")
    assert "<KeepAlive>" in src


def test_panel_exposes_which_template_generation_uses():
    src = (ROOT / "wechat-agent-frontend" / "src" / "views" / "PanelView.vue").read_text(encoding="utf-8")
    assert "default_template" in src, "面板必须能改「生成用哪个模板」,否则新建模板永远选不上"
    assert "设为生成默认" in src


# ---------------------------------------------------------------- 停用过的模板不许"消失"
def test_disabled_template_still_shows_in_the_panel_list(db_path):
    """「停用」是一个**状态**,不是把条目藏起来。

    旧实现 `GET /api/templates` 走 `list_templates()` 的函数默认值 `enabled_only=True`,
    于是点一下「停用」模板就从列表里消失 —— 面板上看不到、也就永远无法重新启用。
    用户看到的是「我明明保存了,列表里却没有」(实测反馈)。
    """
    seed_default_template(str(db_path))
    save_template({"template_id": "tpl_off", "name": "先停用再编辑",
                   "structures": [{"structure_id": "s1", "sections": [{"key": "title", "desc": "标题"}]}]},
                  db_path=str(db_path))
    toggle_template("tpl_off", False, db_path=str(db_path))

    assert "tpl_off" not in [t["id"] for t in list_templates(db_path=str(db_path))], \
        "函数默认值仍是「只看启用」—— 调用方必须显式传 enabled_only=False"
    ids = [t["id"] for t in list_templates(enabled_only=False, db_path=str(db_path))]
    assert "tpl_off" in ids, "停用的模板必须仍然列出来,否则用户没法再把它启用"

    # 路由层必须传这个参数(这才是前端实际走的那条路径)
    router_src = (ROOT / "app" / "web" / "router.py").read_text(encoding="utf-8")
    assert "list_templates(enabled_only=False)" in router_src


def test_resaving_a_disabled_template_re_enables_it(db_path):
    """保存 = 「这是我要用的模板」,必须与 INSERT 分支(enabled=1)口径一致。

    旧实现 ON CONFLICT 分支不碰 enabled,于是「先停用、再编辑保存」的模板停在 0,
    在列表里彻底看不到 —— 表现就是"点了保存却没保存"。
    """
    seed_default_template(str(db_path))
    tpl = {"template_id": "tpl_re", "name": "模板",
           "structures": [{"structure_id": "s1", "sections": [{"key": "title", "desc": "标题"}]}]}
    save_template(tpl, db_path=str(db_path))
    toggle_template("tpl_re", False, db_path=str(db_path))

    tpl["name"] = "模板(改过)"
    save_template(tpl, db_path=str(db_path))

    row = [t for t in list_templates(enabled_only=False, db_path=str(db_path)) if t["id"] == "tpl_re"][0]
    assert row["enabled"] == 1, "重新保存后必须回到启用,否则模板会从面板上消失"
    assert get_template("tpl_re", db_path=str(db_path))["definition"]["name"] == "模板(改过)"
