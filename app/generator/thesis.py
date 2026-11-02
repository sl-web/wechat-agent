"""两步生成的第一步:**只产张力与候选论点,不写文章**。

### 为什么要拆成两步
模型同时"想观点"和"写文章"时,会被写作格式的压力带向稳妥的综述 ——
它的注意力会花在把段落写满、把结构凑齐上,而不是想清楚要说什么。
拆开之后:

1. **第一步(本模块)**只回答"这组素材里最值得说的判断是什么",产出 3~5 个候选论点;
2. **第二步**围绕**人选定的那一个**去写 —— 论点从"可选项"变成**硬约束**。

这和"人工选材"是一脉相承的:**把机器不擅长的判断交给人**。
模型擅长穷举可能性,人擅长判断哪个值得写。

### 为什么还要先提「张力」
只让模型"给个判断",它很容易给出一句**正确但没用**的判断 ——
因为判断可以凭空想。而**张力是素材里的客观事实**:两个都成立的事实摆在一起产生的不协调
(宣称 vs 实际、谁获益 vs 谁买单、数字上的反差)。

先让它把张力指认出来,再要求**每条论点指向某条张力**,论据就不再是空话:
- 论点变成"对张力的回答",而不是另起一个话题;
- 张力本身也随结果返回给界面,人可以看见"这组素材的冲突在哪",并且**可以改选**。

⚠️ 张力是**增强项,不是前置依赖**:模型没给或给得不合格时,只丢掉张力,论点照旧可用。

### 论证姿态从哪来
**取自控制面板的 `article_type` 配置**(`ARGUMENT_MODES` 与写作阶段**共用同一份定义**)。
这一步同样会返回一个 `recommended_type` 供界面显示,但它**默认就是配置的那个值** ——
只有素材明显不适合该姿态时才允许提出别的选择,并要说清理由。
⚠️ 曾经让模型"根据素材自己挑",结果用户配了「行业判断型」,它一路写成「批判质疑型」:
一句"素材里矛盾冲突多 → 更适合批判质疑"就足以推翻配置。**用户显式设的偏好,不能被模型的直觉盖掉。**
"""
from __future__ import annotations

import json
import re

from app.core.config import get_all_config
from app.core.logger import get_logger
from app.generator.llm import LLMClient
from app.generator.prompt import ARGUMENT_MODES
from app.generator.service import (
    GenerationError,
    _material_from_refs,
    _normalize_topic,
    _parse_raw,
)

logger = get_logger("generator.thesis")

# 默认产出几个候选论点(给多了反而难选,4 个刚好)
DEFAULT_COUNT = 4
MAX_COUNT = 6
# 张力条数上限:候选论点数量级的量级,再多界面就成负担了
MAX_TENSIONS = 5
# 短于这个长度的"张力"基本都是"存在挑战"这类空话,直接丢掉
MIN_TENSION_LEN = 12

_TYPE_KEYS = ("industry_judgment", "critical", "teardown", "forecast")
TYPE_LABELS = {
    "industry_judgment": "行业判断型",
    "critical": "批判质疑型",
    "teardown": "深度拆解型",
    "forecast": "趋势预测型",
}


def build_prompt(topic: dict, material: str, n: int = DEFAULT_COUNT,
                 cfg: dict | None = None) -> tuple[str, str]:
    """构造"只出论点"的提示词。返回 (system, user)。

    `cfg` 仅供测试注入配置(生产走 `get_all_config()`)。
    """
    cfg = cfg if cfg is not None else get_all_config()
    persona = str(cfg.get("writer_persona") or "科技观察者").strip()
    stance = str(cfg.get("writer_stance") or "").strip()
    lens = str(cfg.get("writer_lens") or "").strip()
    # ⚠️ 论证姿态必须取自**控制面板的配置**,不能让模型凭素材自由发挥 ——
    # 用户设了「行业判断型」,光靠"素材里矛盾多 → 更适合批判质疑型"这句话,
    # 它就会一路写成批判稿(实测踩过:两步生成的论点与文章全是批判质疑型,配置形同虚设)。
    mode = str(cfg.get("article_type") or "industry_judgment")
    mode_desc = ARGUMENT_MODES.get(mode, ARGUMENT_MODES["industry_judgment"])
    preferred_label = TYPE_LABELS.get(mode, mode)

    type_lines = "\n".join(f"- {k}:{v}" for k, v in TYPE_LABELS.items())
    system = f"""你是公众号选题策划,不是写手。这一轮**只出张力与论点,不写文章**。

【你是谁】
- 身份:{persona}
- 立场:{stance or "对技术叙事保持怀疑,先问谁付钱、谁承担成本"}
- 关注角度:{lens or "优先看商业化落地与成本结构"}
立场决定你**怎么看**这些事实,论点要体现这个视角,而不是中立地罗列。

【第一步:先从素材里找出「张力」】
张力 = 两个**都成立**的事实或诉求摆在一起时产生的不协调。
它不是"这件事有争议",而是**具体到能指认谁和谁不协调、反差在哪**。

优先往这四种形态上靠:
1. **预期差**:都说会变成 X,素材里显示的却是 Y;
2. **利益错位**:一方拿到好处、另一方承担成本,而两边被说成是同一件事;
3. **宣称 vs 实际**:对外说的话,与自己的动作或数字不一致;
4. **数字反差**:把素材里两个数字并排看,不合常理。

❌ 这不是张力:「AI 发展很快,但也面临不少挑战」—— 两边都对,却没有具体冲突,是废话。
✅ 这是张力:「对外讲的是技术普惠,但新计费规则把推理成本推给了付费用户」。

要求:
- 2~{MAX_TENSIONS} 条,**每一条都要能在素材里指认**(谁、什么数字、什么动作);
- 素材里确实找不出张力时宁可少给几条,**不要编**。

【第二步:基于张力给论点】
⚠️ **论证姿态已由人工在控制面板设定为「{preferred_label}」,必须按它来写**:
{mode_desc}

姿态决定论点的**形状**,不要自作主张换成别的姿态。
（下面四种供你理解差异,不是让你四选一:{type_lines}）

其余要求:
1. 是一个**明确到可以被反驳**的判断。说「X 很重要」不算,说「X 的瓶颈不在 A 而在 B」才算。
2. **必须能落到具体事实**:每条论点都要指得出素材里的主体、数字或事件。
3. 每条论点的 `tension_id` 要指向它是从哪条张力里长出来的 ——
   **论点是对张力的回答,不是另起一个话题**。
4. 尽量让不同论点覆盖不同张力;{n} 条**互相不同**,不要同一句话说四遍。
5. 不要综述腔。禁止「值得关注」「影响深远」「意义重大」这类没有立场的空话。

【文章类型推荐(默认就是配置的那个)】
`recommended_type` **默认填 `{mode}`**,`type_reason` 说明它为什么适合这组素材。
**只有**素材明显不适合这个姿态时才提出别的选择(例如素材里根本没有可验证的时间表,
却要写「趋势预测型」),并在 `type_reason` 里说清理由 —— 这是例外,不是常态。

【输出格式】
直接输出一个 JSON 对象,**不要任何解释文字、不要用 ``` 包裹**:
{{
  "tensions": [
    {{
      "id": "t1",
      "tension": "一句话说清冲突(30~60 字)",
      "sides": "谁和谁不协调(20 字内,如「厂商 vs 付费用户」)",
      "contrast": "声称是什么、实际是什么(30 字内)",
      "evidence": "支撑它的具体事实,必须含主体或数字"
    }}
  ],
  "recommended_type": "默认填 {mode}",
  "type_reason": "为什么这组素材适合这种写法(一句话,30 字内)",
  "theses": [
    {{
      "thesis": "一句可被反驳的判断(40~80 字)",
      "tension_id": "它来自哪条张力,填上面的 id,如 t1",
      "support": "支撑它的素材事实,必须含具体主体或数字",
      "angle": "从什么角度切入(15 字内)",
      "type": "这一条的论证姿态,默认与 recommended_type 相同",
      "hook": "开头能抓人的反常细节或数字(30 字内)"
    }}
  ]
（上面是**字段说明**,实际输出时把注释换成真实内容。共给出 2~{MAX_TENSIONS} 条张力、
{n} 条论点;`tension_id` 必须能在上面的 tensions 里找得到。)

【事实性硬约束】
只基于下方素材,禁止编造数字、人名、时间。素材不足就写「据公开报道」,
不要为了让张力或论点好看而编细节。"""

    user = f"""【选题】{topic.get('title', '')}

【素材(仅这些可作为事实依据)】
{material}

【任务】先从素材里提取张力,再基于张力给出 {n} 条候选论点,并推荐一种最适合的文章类型。"""
    return system, user


def _pick_type(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in _TYPE_KEYS:
        return s
    for k, label in TYPE_LABELS.items():
        if label in str(raw or ""):
            return k
    return ""


def generate(topic: dict, related: list[dict] | None = None,
             analysis: dict | None = None, n: int = DEFAULT_COUNT,
             llm: LLMClient | None = None, cfg: dict | None = None) -> dict:
    """生成候选论点 + 推荐文章类型。**不落库、不写文章。**

    `topic` 可以是热点行,也可以是分析报告的聚类主题(`_normalize_topic` 负责统一)。
    失败时抛 `GenerationError`(由上层翻译成可执行的提示)。
    `cfg` 仅供测试注入配置(生产走 `get_all_config()`)。
    """
    n = max(1, min(int(n or DEFAULT_COUNT), MAX_COUNT))
    cfg = cfg if cfg is not None else get_all_config()
    mode = str(cfg.get("article_type") or "industry_judgment")
    t = _normalize_topic(topic)
    if not (t.get("title") or "").strip():
        raise GenerationError("没有可用的选题:请先在分析报告里勾选素材")

    material = _material_from_refs({
        "main_topic": t,
        "related_topics": related or [],
        "analysis": analysis or {},
    })
    if len(material.strip()) < 20:
        material = f"标题: {t.get('title', '')}\n摘要: {t.get('summary', '')}\n链接: {t.get('url', '')}"

    system, user = build_prompt(t, material, n, cfg=cfg)
    client = llm or LLMClient()
    try:
        content = client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.8,
        )
    except Exception as e:  # noqa: BLE001
        raise GenerationError(f"生成候选论点失败: {e}") from e

    obj = _parse_raw(content)
    if not isinstance(obj, dict):
        raise GenerationError("模型返回的不是 JSON 对象,可重试一次")
    # ⚠️ 这里**不能**用 `_coerce_fields` —— 它的职责是把数组拼成字符串
    # (防止文章字段被模型返回成列表),而 theses 本身就是数组,会被它毁掉。
    # 只对顶层标量字段做兜底转换。
    for k in ("recommended_type", "type_reason"):
        v = obj.get(k)
        if isinstance(v, list):
            obj[k] = str(v[0]) if v else ""
        elif v is None or isinstance(v, (int, float)):
            obj[k] = str(v) if v is not None else ""

    # 张力是增强项:解析不出来就空着,不能因为它让整步失败
    tensions = _parse_tensions(obj.get("tensions"))
    tension_ids = {t["id"] for t in tensions}

    theses = obj.get("theses")
    if isinstance(theses, str):                      # 模型偶尔把数组写成字符串
        theses = _parse_list_like(theses)
    if not isinstance(theses, list):
        raise GenerationError("模型没有按要求返回候选论点列表,可重试一次")

    out: list[dict] = []
    for item in theses[:n]:
        if isinstance(item, str):
            item = {"thesis": item}
        if not isinstance(item, dict):
            continue
        # 单个字段可能是数组(模型习惯),统一取第一条
        item = {k: (v[0] if isinstance(v, list) and v else v) for k, v in item.items()}
        thesis = str(item.get("thesis") or "").strip()
        if len(thesis) < 8:                          # 太短的多半是空话或占位
            continue
        # 指向不存在的张力就置空 —— 前端会显示"未关联张力",而不是指向一条假张力
        tid = str(item.get("tension_id") or "").strip()
        out.append({
            "thesis": thesis,
            "tension_id": tid if tid in tension_ids else "",
            "support": str(item.get("support") or "").strip()[:300],
            "angle": str(item.get("angle") or "").strip()[:60],
            # 姿态已由配置统一决定,单条留空就回落到它 —— 前端只在"模型提出例外"时才显示这一项
            "type": _pick_type(item.get("type")) or mode,
            "hook": str(item.get("hook") or "").strip()[:120],
        })
    if not out:
        raise GenerationError("模型返回的候选论点都不可用(过短或为空),可重试一次")

    rec = _pick_type(obj.get("recommended_type"))
    return {
        "tensions": tensions,
        "theses": out,
        "recommended_type": rec,
        "recommended_label": TYPE_LABELS.get(rec, ""),
        "type_reason": str(obj.get("type_reason") or "").strip()[:200],
        "material_chars": len(material),
    }


def _parse_tensions(raw) -> list[dict]:
    """规范化张力列表。

    张力是**增强项**:模型没给、或给得不合格时返回空列表,调用方照旧用论点 ——
    不能因为"这次没提取到张力"就让整个第一步失败。

    id 缺失或重复时重新编号(补成 t1/t2…),保证论点能稳定指向某一条。
    """
    if isinstance(raw, str):                          # 模型偶尔把数组写成字符串
        raw = _parse_list_like(raw)
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            item = {"tension": item}
        if not isinstance(item, dict):
            continue
        # 单个字段可能是数组(模型习惯),统一取第一条
        item = {k: (v[0] if isinstance(v, list) and v else v) for k, v in item.items()}
        tension = str(item.get("tension") or "").strip()
        if len(tension) < MIN_TENSION_LEN:            # 短的多半是"存在挑战"这类空话
            continue
        tid = str(item.get("id") or "").strip()
        if not tid or any(t["id"] == tid for t in out):
            tid = f"t{len(out) + 1}"
        out.append({
            "id": tid,
            "tension": tension[:200],
            "sides": str(item.get("sides") or "").strip()[:60],
            "contrast": str(item.get("contrast") or "").strip()[:120],
            "evidence": str(item.get("evidence") or "").strip()[:300],
        })
        if len(out) >= MAX_TENSIONS:
            break
    return out


def _parse_list_like(text: str) -> list:
    """模型把数组写成字符串时的兜底:尽量把里面的对象抠出来。"""
    try:
        v = json.loads(text)
        return v if isinstance(v, list) else []
    except ValueError:
        pass
    items = re.findall(r"\{[^{}]*\}", text)
    out = []
    for s in items:
        try:
            out.append(json.loads(s))
        except ValueError:
            continue
    return out
