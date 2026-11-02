"""标题候选:一篇文章写完后,给几个**形态不同**的标题让人挑。

### 为什么要单做这一步
打开率决定阅读量,而**转发的前提是被打开**。实测数据里高分享的标题有很明显的形态偏好
(疑问式、「为什么…？」、短标题),而模型默认只会给一个"稳妥的标准标题"。
让人在几个成型的方向里挑,比"不满意就整篇重新生成"便宜得多(重生成一次要十几秒)。

### 传播潜力分
每个候选会顺带用 `app/analyzer/growth.py` 的画像标一个**传播潜力分** ——
画像若来自账号自己的转发数据,这个分就有参考价值。它只帮排序,**不做取舍**。
"""
from __future__ import annotations

import json
from typing import Any

from app.core.config import get_all_config
from app.core.logger import get_logger
from app.generator.llm import LLMClient
from app.generator.thesis import GenerationError, _parse_raw

logger = get_logger("titles")

DEFAULT_COUNT = 3
MAX_COUNT = 5
_MIN_TITLE_LEN = 6
_MAX_TITLE_LEN = 40

# 形态要"不同",否则三个标题只是同一句话换个说法,选不出东西
_FORMS = (
    "疑问式(问一个读者能代入的具体问题)",
    "反常识判断(直接给一个会被反驳的结论)",
    "数字或反差(用文章里真实存在的数字/对比)",
    "悬念留白(点出异常但不解释原因)",
    "主体锚点(突出文章里的具体人物/机构/产品)",
)

SYSTEM_TMPL = """你是公众号标题编辑,不是作者。下面是一篇**已经写好的**文章,
你只负责给它出标题,不要改内容、不要写导语。

【硬性要求】
1. 出 {n} 个候选,**每个的形态必须不同**。可选形态:
{forms}
2. **必须忠于文章内容** —— 不许编造文章里没有的数字、人名、事件。
3. **不做失真标题党**:禁止「震惊」「必看」「史上最强」「看完泪目」这类词。
   微信会限流,读者也会反感 —— 它反而降低打开率。
4. 每个标题 **25 字以内**(手机上一行才显示得下)。
5. 不要把整句用引号或书名号包起来。
6. 疑问式要问**读者答得上来、有代入感**的具体问题,不要写"你怎么看"这种没法回答的。

【输出格式】
直接输出一个 JSON 对象,**不要任何解释文字、不要用 ``` 包裹**:
{{
  "titles": [
    {{"title": "标题正文(不带引号)", "form": "形态名(两个字以内)",
      "reason": "为什么这样更可能被点开或转发(25 字内)"}}
  ]
}}"""


def build_prompt(article: dict, n: int) -> tuple[str, str]:
    forms = "\n".join(f"   - {f}" for f in _FORMS)
    system = SYSTEM_TMPL.format(n=n, forms=forms)
    body = str(article.get("body") or "")
    digest = body[:400].replace("\n", " ")
    user = (
        f"【原标题】{article.get('title', '')}\n"
        f"【核心论点】{article.get('thesis', '') or '(未填)'}\n"
        f"【导语】{article.get('lead', '') or '(未填)'}\n"
        f"【金句】{article.get('quote', '') or '(未填)'}\n"
        f"【正文梗概】{digest}"
    )
    return system, user


def _clean_title(s: Any) -> str:
    t = str(s or "").strip()
    # 模型常把整句包进引号/书名号,去掉外层包装
    for a, b in (("「", "」"), ("“", "”"), ('"', '"'), ("《", "》"), ("'", "'")):
        if t.startswith(a) and t.endswith(b) and len(t) > 2:
            t = t[1:-1].strip()
    return t


def suggest(article: dict, n: int = DEFAULT_COUNT, llm: LLMClient | None = None,
            profile: dict | None = None) -> dict:
    """给一篇文章出 n 个候选标题(形态不同)。

    返回 {titles: [{title, form, reason, virality}], current_title, note}
    `virality` 是传播潜力分(0~1,0.5 = 与账号历史平均持平;无画像时恒为 0.5)。

    `profile` 不传则自动读全局传播画像 —— 显式传入是为了让测试可确定。
    """
    n = max(1, min(int(n or DEFAULT_COUNT), MAX_COUNT))
    if not str(article.get("title") or "").strip() and not str(article.get("body") or "").strip():
        raise GenerationError("这篇文章还没有内容,无法生成标题")

    system, user = build_prompt(article, n)
    client = llm or LLMClient()
    try:
        content = client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.8,
        )
    except Exception as e:  # noqa: BLE001
        raise GenerationError(f"生成候选标题失败: {e}") from e

    obj = _parse_raw(content)
    if not isinstance(obj, dict):
        raise GenerationError("模型返回的不是 JSON 对象,可重试一次")

    raw = obj.get("titles")
    if isinstance(raw, str):                 # 模型把数组写成字符串时的兜底
        try:
            parsed = json.loads(raw)
            raw = parsed if isinstance(parsed, list) else []
        except ValueError:
            from app.generator.thesis import _parse_list_like

            raw = _parse_list_like(raw)
    if not isinstance(raw, list):
        raw = []

    # 传播画像:顺带给每个候选打分,供人工参考(不参与取舍)
    if profile is None:
        try:
            from app.analyzer.growth import load_profile

            profile = load_profile()
        except Exception as e:  # noqa: BLE001
            logger.warning("传播画像不可用,候选标题不显示传播分: %s", e)
            profile = {}
    profile = profile if isinstance(profile, dict) else {}

    def scorer(t: str) -> float:
        try:
            from app.analyzer.growth import growth_score

            return growth_score(t, profile)
        except Exception:  # noqa: BLE001
            return 0.5

    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean_title(item.get("title"))
        if not (_MIN_TITLE_LEN <= len(title) <= _MAX_TITLE_LEN) or title in seen:
            continue
        seen.add(title)
        out.append({
            "title": title,
            "form": str(item.get("form") or "").strip()[:8],
            "reason": str(item.get("reason") or "").strip()[:60],
            "virality": scorer(title),
        })
        if len(out) >= n:
            break

    if not out:
        raise GenerationError("模型返回的候选标题都不可用(过短或重复),可重试一次")

    return {
        "titles": out,
        "current_title": str(article.get("title") or ""),
        "profile_used": bool(profile.get("ok")),
        "note": "传播潜力分来自账号自己的转发数据(0.5 = 与历史平均持平);仅供参考,不做取舍。",
    }
