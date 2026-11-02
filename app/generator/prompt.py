"""提示词组装(PRD 5.3.2)。

四层约束,缺一不可:
1. **事实性硬约束** —— 防幻觉,判断可以主观,事实必须来自素材;
2. **作者立场** —— 决定「谁在说」。没有立场就只有综述,没有观点;
3. **论点要求**(仅论点驱动结构)—— 强制表态:先给判断、必须可被反驳、要写反方;
4. **正文排版要求** —— 强制输出 Markdown 小标题与加粗。

第 4 条是后加的,原因是实测发现:**模型几乎从不输出任何 Markdown 结构**
(`##` 计数为 0、`**` 计数为 0),于是生成的正文在公众号里是一整块灰,
没有任何视觉层级。而 prompt 里原先写着"不要输出任何 Markdown" ——
那句话本意是约束 JSON 外层,却被模型理解成正文也不能用 Markdown。
现在明确区分「JSON 外层」与「body 字段内部」。
"""
from __future__ import annotations

from app.core.config import get_all_config
from app.generator.templates import get_template
from app.generator.validator import resolve_ban_words

# 事实性硬约束(所有生成请求强制携带)
FACT_CONSTRAINTS = """
【事实性硬约束(必须遵守)】
1. 只基于下文提供的热点事实与素材写作,禁止编造。
2. 关键数字、人名、时间若素材中未提供,禁止编造;应写"据公开报道"并标注【待核实】留待人工补充。
3. 正文末尾自动生成"信息来源"列表,引用下方提供的原文链接。
4. 你无法确认的事实一律不得断言。
5. **观点可以主观,事实必须可查**。允许有倾向和态度,但每一个论据都要能对应到素材中的具体内容。
"""

# 正文排版规范(所有结构强制携带)。
# 这段决定文章在手机上"读得下去",不是可选项。
LAYOUT_RULES = """
【正文排版规范(body 字段内部必须遵守)】
你的 body 会被直接转成公众号图文。手机上读者扫一眼就决定要不要看下去 ——
**没有小标题、没有加粗的正文就是"一坨灰",没人读得完。**

1. **每个小节必须以 `## ` 开头**,写在独立一行,例如 `## ②套餐分层对应算力配给`。
   ⚠️ 绝对不要把 `①` `②` `一、` `1.` 这类序号当成段落开头写在正文里 ——
   那样在排版上没有视觉层级。序号要和小标题写在一起,并加 `## ` 前缀。
2. **每个小节里至少有一处 `**加粗**`**,加粗的是**判断句或关键数字**,不是普通名词。
3. **段落要短**:每段 60~150 字,**段与段之间空一行**(即 `\\n\\n`)。
   一段超过 250 字必须拆成两段。
4. 需要并列列举时,用 `- ` 开头的无序列表。
5. **不要**写文章大标题(不要用 `#`),标题由 title 字段负责。
6. JSON 字符串里换行请写 `\\n`,确保 `##` 前有空行。
"""

# body 排版的正误对照 —— 给例子比给规则有效得多
LAYOUT_EXAMPLE = """
【body 正误对照】
✗ 错误(流水段:序号只是文本,没有任何层级)
   ①计费规则暴露成本焦虑。语音产品负责人明确,语音模式将问题转交底层文本模型时均按一条消息计费。……
   ②套餐分层对应算力配给。公开规则显示,Go 套餐仅提供……

✓ 正确(每个小节有 `## ` 小标题,判断句加粗,段落之间有换行)
   ## ①计费规则暴露成本焦虑
   语音产品负责人明确,语音模式将问题转交底层文本模型时,均按一条消息计费。

   **把重模型调用严格按条计价,本质是给算力装一道阀门。**

   ## ②套餐分层对应算力配给
   公开规则显示,Go 套餐仅提供最长 3 小时的使用时长,而 Pro 套餐开放完整模型调用。
"""


# 论证姿态 → 具体要怎么写(由配置 article_type 选择)
ARGUMENT_MODES = {
    "industry_judgment": (
        "行业判断型:回答「这件事说明了什么趋势,依据是什么」。"
        "论点要是一个关于行业走向的判断,并给出可验证的依据。"
    ),
    "critical": (
        "批判质疑型:审视主流叙事。先准确复述最流行的看法,再指出它站不住脚的地方"
        "(数据漏洞、利益动机、被忽略的反例)。不要为反对而反对 —— 确实有漏洞才批评。"
    ),
    "teardown": (
        "深度拆解型:把这件事拆开看真正的运转逻辑(钱从哪来、谁做什么、卡在哪一环)。"
        "重点在「机制」而不是「态度」。"
    ),
    "forecast": (
        "趋势预测型:基于当前事实做出可被事后检验的预测,"
        "必须写清「什么条件下成立」「出现什么信号说明我错了」。"
    ),
}

_DEFAULT_SECTIONS = [
    {"key": "title", "desc": "标题"},
    {"key": "lead", "desc": "导语"},
    {"key": "body", "desc": "正文 1200~1800 字"},
    {"key": "quote", "desc": "金句"},
    {"key": "cta", "desc": "结尾引导"},
]


def _sections_of(template: dict, structure_id: str) -> tuple[list[dict], dict]:
    """返回 (sections, structure)。"""
    definition = template.get("definition", {})
    structs = definition.get("structures", [])
    structure = next((s for s in structs if s["structure_id"] == structure_id), None)
    if not structure:
        return list(_DEFAULT_SECTIONS), {}
    return structure.get("sections") or list(_DEFAULT_SECTIONS), structure


# 人工选定论点时追加的硬约束(两步生成的第二步)
CHOSEN_THESIS_BLOCK = """
【本轮核心论点(已由人工选定,必须围绕它写)】
{thesis}

这不是建议,而是**硬约束**:
1. 全文都在论证这个判断,不要另起炉灶换成别的观点;
2. `thesis` 字段原样写回这句话(措辞可以润色,但判断本身不能改);
3. 每一条论据都要服务于它 —— 不能有哪一节讲着讲着跑到别的话题上;
4. 如果素材里出现了对你这个判断不利的事实,**正面处理它**(解释为什么它不成立,
   或承认边界条件),不要装作没看见。
"""


# 人工选定「张力」时追加的硬约束(与论点互补:张力是冲突,论点是对冲突的回答)
CHOSEN_TENSION_BLOCK = """
【本轮要写的张力(已由人工选定,全文围绕它展开)】
{tension}

这不是建议,而是**硬约束**:
1. 全文的冲突主线就是它 —— 每一节都要服务于"把这个不协调讲清楚";
2. **前 1/3 之内**就要让读者感知到这个反差,不要留到最后才抖出来;
3. 如果素材里出现了削弱这个张力的事实,**正面处理它**(解释它为什么不能推翻,或限定边界),
   不要装作没看见,也不要含糊带过;
4. 不要写成"机遇与挑战并存"式的两头讨好 —— 那种文章没有张力,只有平衡。
"""


# 转发钩子:公众号涨粉的**唯一放大器是分享**(实测约 1 次分享 ≈ 15~20 阅读,
# 而微信推荐流量平均只有 1.2)。所以文章不能只"写得好",必须给读者**转出去的理由**。
# 由配置 share_hooks_enabled 控制(默认开),可随时关掉回到纯内容逻辑。
SHARE_HOOKS_INTRO = """【转发钩子(本项强制 —— 它直接决定这篇有没有人看到)】
公众号的读者增长**几乎只靠分享**:读者转发到朋友圈,才会带来他朋友那一层新读者。
所以除了"写清楚",还必须让读者**有理由转出去**。硬要求:"""

# ⚠️ 三条钩子**只在该结构真的有这个字段时才下发**。
# 真实踩过:结构 s2 盘点式没有 `quote` 字段,却一直被要求"quote 必须能被单独转发",
# 模型只能凭空造一个用不上的 quote,同时把真正需要的 `cta` 写丢
# (实测 3 次重试里 2 次报「缺少章节: ['cta']」)。
_SHARE_HOOK_ITEMS: tuple[tuple[str, str], ...] = (
    ("quote", """**`quote` 必须是一句能被单独摘出来转发的句子**。
   读者转发时往往只带这句话,所以它必须:40 字以内、有明确态度、**脱离上下文也读得懂**。
   ✗ 不能写摘要式的空话(如"综上所述,行业正在发生变化");
   ✓ 要像"把算力成本转嫁给付费用户,才是这轮涨价真正的动作" —— 一句能当观点用的话。"""),
    ("cta", """**`cta` 必须同时给出「转发的理由」和「关注的理由」**,并用一个具体问题引导留言:
   - 转发理由:转出去对读者有什么好处(说出了他想说的话 / 转给同事有用 / 显得有见识);
   - 关注理由:关注你能**持续拿到什么**,要具体到内容方向,不要写"欢迎关注"这种空话;
   - 留言引导:提一个**读者答得上来**的具体问题 ——
     ✗ "你怎么看?"(太大,没人答);✓ "你们公司今年的算力预算砍了吗?"(有具体对象)。
   ⚠️ 留言是账号权重的重要信号:实测 56 篇里**只有 5 篇**有留言,这块完全没做起来。"""),
    ("body", """**`body` 里必须至少有一处可被收藏的「干货」** —— 一段读者能直接抄走的清单或判断标准
   (用 `- ` 列表)。让读者"看不完先收藏"。收藏/在看会显著提升平台给你的推荐量。"""),
)


def build_share_hooks(sections: list[dict]) -> str:
    """按模板结构里**实际存在**的字段组装转发钩子要求(没有的字段就不提)。"""
    keys = {str(s.get("key") or "") for s in (sections or [])}
    items = [text for key, text in _SHARE_HOOK_ITEMS if key in keys]
    if not items:
        return ""
    numbered = "\n\n".join(f"{i}. {text}" for i, text in enumerate(items, 1))
    return f"{SHARE_HOOKS_INTRO}\n\n{numbered}\n"


# 重试时回灌上一稿的问题(靶向改,而不是盲重写一遍)
RETRY_ISSUES_BLOCK = """
【上一稿被判不合格,这一稿必须针对性改掉】
{issues}

注意:这不是让你换个话题重写一遍 —— **论点、张力、结构都保持不变**,
只修正上面列出的问题。修完再自己核对一遍是否真的不再命中。
"""


def build_system_prompt(template: dict, structure_id: str,
                        overrides: dict | None = None,
                        chosen_thesis: str = "",
                        chosen_tension: str = "",
                        retry_issues: list[str] | None = None) -> str:
    """从模板 + 配置的写作立场生成 system prompt。

    `overrides`: 本次调用专用的配置覆盖(如用户在界面上选的 article_type),
    优先级高于全局配置但**不写回**配置 —— 只影响这一篇。
    `chosen_thesis`: 两步生成里人工选定的论点,传了就变成硬约束。
    `chosen_tension`: 两步生成里人工选定(或改选)的张力,传了就变成硬约束。
    `retry_issues`: 上一稿没过审的具体原因,重试时回灌给模型做**靶向修改**。

    配置 `share_hooks_enabled`(默认开)控制「转发钩子」块 ——
    公众号涨粉几乎全靠分享,关掉等于只写字不传播。
    """
    definition = template.get("definition", {})
    style = definition.get("style", {})
    sections, structure = _sections_of(template, structure_id)
    must_stance = bool(structure.get("must_take_stance"))

    cfg = get_all_config()
    if overrides:
        cfg = {**cfg, **{k: v for k, v in overrides.items() if v not in (None, "")}}
    persona = str(cfg.get("writer_persona") or style.get("persona") or "科技观察者").strip()
    stance = str(cfg.get("writer_stance") or "").strip()
    lens = str(cfg.get("writer_lens") or "").strip()
    mode = str(cfg.get("article_type") or "industry_judgment")

    # JSON 骨架按 sections **动态生成** —— 以后加字段不用再改这里
    json_skeleton = "{\n" + ",\n".join(
        f'  "{s["key"]}": "..."' for s in sections
    ) + "\n}"

    def _section_line(s: dict) -> str:
        """一节的说明(+ 显式字数目标)。

        字数目标必须**明说**:描述里的数字可能指的是"每条/每段"(如 s2 的
        「每条 150~250 字」),模型分不清会写出只有几百字的正文,validator 也会判不合格。
        ⚠️ 这条与 `validator._guess_len_range` 用同一对 min_len/max_len,别只改一边。
        """
        line = f"  - {s['key']}: {s['desc']}"
        lo, hi = s.get("min_len"), s.get("max_len")
        if isinstance(lo, int) and isinstance(hi, int) and 0 < lo < hi:
            line += f"(本字段总长度 {lo}~{hi} 字,按含标点字符数算)"
        return line

    sec_desc = "\n".join(_section_line(s) for s in sections)

    stance_block = ""
    if persona or stance or lens:
        stance_block = f"""
【你的身份与立场(动笔时始终带着它)】
- 身份:{persona}
- 立场:{stance}
- 关注角度:{lens}
立场决定你**怎么看**这些事实:同一个事实,不同视角会得出不同结论。
请写出「你这个视角下」的判断,而不是中立地罗列所有说法。
"""

    argument_block = ""
    if must_stance:
        # ⚠️ 第 3 条必须**随姿态变化**,不能写成通用要求:
        # 只有「批判质疑型」才是"写出主流看法并反驳"。把它当成所有姿态都要满足的条件,
        # 会把「行业判断型」也逼成批判稿 —— 实测踩过:用户配了行业判断型,
        # 论点与文章仍然是批判质疑的口吻。
        if mode == "critical":
            stance_req = ("3. **正面写出最流行的看法,并说明你为什么不同意**。"
                          "没有反方的文章不是分析,是通稿。")
        elif mode == "teardown":
            stance_req = ("3. 先写清通行说法对这件事运转机制的解释,再指出它**漏掉了哪一环** —— "
                          "拆解靠的是机制,不是态度。")
        else:      # industry_judgment / forecast:不要求站到主流的对立面
            stance_req = ("3. **先写出主流预期或通行说法,再给出你的判断** —— 可以赞同、可以修正,"
                          "但必须说清你的判断与通行说法差在哪,不能只复述共识。")
        argument_block = f"""
【论点要求(本结构强制)】
{ARGUMENT_MODES.get(mode, ARGUMENT_MODES["industry_judgment"])}

必须做到:
1. **先亮判断,再给依据**。论点出现在全文前 1/3,不要铺垫半天才表态。
2. 论点必须**可被反驳** —— 如果一句话没人能反对,它就是废话(如"影响深远""值得关注""意义重大")。
{stance_req}
4. 每条论据都要落到素材中的**具体事实、数字或主体**,不要用形容词代替证据。
"""
    # 张力块排在论点块之前:先让模型知道"冲突在哪",再告诉它"你的判断是什么"
    tension_block = CHOSEN_TENSION_BLOCK.format(tension=chosen_tension.strip()) \
        if chosen_tension.strip() else ""
    thesis_block = CHOSEN_THESIS_BLOCK.format(thesis=chosen_thesis.strip()) \
        if chosen_thesis.strip() else ""
    retry_block = ""
    if retry_issues:
        items = "\n".join(f"- {str(x)[:200]}" for x in retry_issues if str(x).strip())
        if items:
            retry_block = RETRY_ISSUES_BLOCK.format(issues=items)
    # 转发钩子(可关):不管内容多好,没人转就没有新读者。只下发该结构真有的字段。
    share_block = build_share_hooks(sections) if cfg.get("share_hooks_enabled", True) else ""
    return f"""你是公众号作者。文风:{style.get('tone', '专业')}。
{stance_block}{argument_block}{tension_block}{thesis_block}{share_block}{retry_block}{LAYOUT_RULES}{LAYOUT_EXAMPLE}
【输出格式要求(必须严格遵守)】
直接输出一个 JSON 对象。**JSON 的外层**不要有任何解释文字、不要用 ``` 代码块包裹。
注意:「不要 Markdown」只约束 **JSON 外壳**;`body` 字段的**内容内部必须**
包含上面要求的 Markdown 结构(`## 小标题` 与 `**加粗**`),否则文章排版会失效。
格式如下:
{json_skeleton}

【目标结构说明】
{sec_desc}

【风格红线】避免使用违禁词: {resolve_ban_words(definition)}

{FACT_CONSTRAINTS}"""


def build_user_prompt(topic: dict, structure_id: str, extra_material: str = "",
                      template: dict | None = None) -> str:
    """从选定的热点构造 user prompt。"""
    material = extra_material or (
        f"标题: {topic.get('title', '')}\n"
        f"摘要: {topic.get('summary', '')}\n"
        f"原文链接: {topic.get('url', '')}\n"
        f"来源: {topic.get('source', '')}"
    )

    # 论点驱动结构:要求「先想清楚再写」。
    # 模型同时做"想观点"和"写文章"时,会被写作格式的压力带向稳妥的综述;先逼它想三个问题可避开。
    think_block = ""
    if template:
        sections, _ = _sections_of(template, structure_id)
        if any(s["key"] == "thesis" for s in sections):
            think_block = """
【动笔前先想清楚(不用输出,但决定文章成败)】
1. 这组素材里**最反直觉、最矛盾**的一点是什么?—— 观点从这里来。
2. 最流行的解读是什么?它忽略了什么,或者谁在推动这个解读?
3. 这件事里,谁获利、谁承担成本?
"""
    return f"""【写作主题】{topic.get('title', '')}
{think_block}
【可用素材(仅这些可作为事实依据)】
{material}

【任务】按输出格式要求生成 JSON,把所有字段写完整(不要留空)。若素材不足,禁止编造,相关位置标注【待核实】。"""


def assemble_prompt(
    template_id: str,
    structure_id: str,
    topic: dict,
    extra_material: str = "",
    db_path: str | None = None,
    overrides: dict | None = None,
    chosen_thesis: str = "",
    chosen_tension: str = "",
    retry_issues: list[str] | None = None,
) -> tuple[str, str]:
    """一键组装,返回 (system_prompt, user_prompt)。

    `overrides` / `chosen_thesis` / `chosen_tension` 供两步生成使用:
    用户选定的文章类型、论点与张力只影响这一篇,不写回全局配置。
    `retry_issues`: 上一稿没过审的原因,重试时回灌做靶向修改。
    """
    template = get_template(template_id, db_path=db_path)
    if not template:
        raise ValueError(f"模板不存在: {template_id}")
    sys_p = build_system_prompt(template, structure_id, overrides,
                                chosen_thesis, chosen_tension, retry_issues)
    usr_p = build_user_prompt(topic, structure_id, extra_material, template=template)
    return sys_p, usr_p
