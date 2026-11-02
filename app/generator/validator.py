"""质量校验器(PRD 5.3.3):字数/结构/敏感词/事实性 + 反综述护栏。

反综述护栏存在的理由:
LLM 面对一堆并列的新闻事实,默认行为是"归纳总结"。光在提示词里要求它有观点是不够的 ——
必须**可自动检查**,不达标就判不合格并重新生成,才能把下限抬起来。
"""
from __future__ import annotations

import re

from app.core.config import get_config
from app.core.db import connect
from app.generator.templates import get_template

# 默认违禁词(广告法极限词等)。
# 注意:必须是**词组**而不是单字 —— 单个"最"会把"最关键""最重要的变量"这类
# 完全正常的分析表达全部误杀(真实踩过:一篇好文章因此被熔断)。
BAN_WORDS_DEFAULT = ["最好", "最佳", "最优", "最强", "第一品牌", "国家级",
                     "世界级", "顶级", "万能", "100%", "绝对"]

# 合法搭配豁免:这些词里含有极限词,但在技术/分析写作里是**术语或通用表达**,
# 不该按广告法极限词判死。
# 真实踩过:一篇正常的行业分析因为写了「最优解」被判「命中违禁词: ['最优']」,
# 触发了**整篇重写**(每次要 150 秒以上),用户看到的现象是"生成卡住不动"。
# 判违禁词前先把这些搭配摘掉,再查剩下的部分。
BAN_EXEMPT = (
    "最优解", "次优解", "最优化", "帕累托最优", "最优路径",
    "最佳实践", "最佳路径",
)


def strip_ban_exempt(text: str) -> str:
    """摘掉合法搭配,避免其中的极限词被误判。"""
    for w in BAN_EXEMPT:
        text = text.replace(w, "")
    return text


def resolve_ban_words(template: dict | None = None) -> list[str]:
    """违禁词的**唯一来源**:prompt 的红线提示与 validator 的判定必须用同一张表。

    以前两边口径不一致(prompt 读模板里的 `style.ban_words`,validator 用
    `BAN_WORDS_DEFAULT`)→ 模型被要求避开 A,却被按 B 判罚,怎么改都可能不过。
    """
    style = ((template or {}).get("definition") or {}).get("style") or {}
    words = style.get("ban_words")
    if isinstance(words, list):
        cleaned = [str(w).strip() for w in words if str(w).strip()]
        if cleaned:
            return cleaned
    return list(BAN_WORDS_DEFAULT)

# 综述式开头:典型的"信息播报"腔调。出现在**开头**即判为综述。
RECAP_OPENERS = ("近日", "据悉", "随着", "近年来", "众所周知", "日前", "据报道", "前不久")
# 立场标记:出现其一才算"表过态"
STANCE_MARKERS = (
    "我认为", "我的判断", "真正的", "问题在于", "说到底", "这意味着", "更值得",
    "关键在于", "我不这么看", "并不", "恰恰", "与其说", "本质上是", "被忽略",
    "很少有人", "我更愿意", "真正需要", "这反映出", "暴露出", "掩盖了",
)
# 转折标记:分析文必须有反方或意外(对比与否定也算)
TURN_MARKERS = ("但", "然而", "不过", "相反", "恰恰", "事实上", "其实",
                "而不是", "并不", "却", "并非")
# 空洞论点:有形式无内容,没人能反对 = 等于没有观点
EMPTY_CLAIMS = (
    "值得关注", "影响深远", "意义重大", "备受瞩目", "引发关注",
    "值得期待", "不容忽视", "大有可为", "具有里程碑意义",
)

# 描述里"限定单条/单段"的字数词:它后面的数字区间**不是全文长度**,
# 拿去判全文会把长文全部误杀(见 _guess_len_range 的说明)。
PER_ITEM_HINTS = ("每条", "每节", "每段", "每个", "每部分", "每项", "单条", "段落", "小标题")


class Validator:
    def __init__(
        self,
        template_id: str,
        structure_id: str,
        ban_words: list[str] | None = None,
        db_path: str | None = None,
    ):
        self.template_id = template_id
        self.structure_id = structure_id
        self.db_path = db_path
        # 先取模板:违禁词表以模板为准(与 prompt 的红线提示同源,避免两套口径)
        self._template = get_template(template_id, db_path=db_path)
        self.ban_words = ban_words or resolve_ban_words(self._template)

    def _sections(self) -> list[dict]:
        if not self._template:
            return []
        structs = self._template.get("definition", {}).get("structures", [])
        st = next((s for s in structs if s["structure_id"] == self.structure_id), None)
        return st["sections"] if st else []

    def _structure(self) -> dict:
        if not self._template:
            return {}
        structs = self._template.get("definition", {}).get("structures", [])
        return next((s for s in structs if s["structure_id"] == self.structure_id), {}) or {}

    def _must_take_stance(self) -> bool:
        return bool(self._structure().get("must_take_stance"))

    def _guard_on(self) -> bool:
        """反综述护栏是否开启。

        ⚠️ 必须从**本实例指向的那个库**读配置 —— 旧实现 `get_config(..., None)` 读的是
        默认库,于是带 `db_path` 跑(测试、隔离环境)时会读到另一个库的设置:
        模板来自 A 库、护栏开关来自 B 库,两套口径。
        """
        try:
            conn = connect(self.db_path)
            try:
                return bool(get_config("anti_recap_guard", conn) is not False)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return True      # 读不到就按"开"处理,护栏宁可生效

    def validate(self, result: dict) -> dict:
        """result: LLM 解析出的结构化稿件 {title, lead, body, quote, cta[, thesis]}
        返回: {"ok": bool, "issues": [str], "fact_notes": [str]}
        """
        issues: list[str] = []

        def _s(v) -> str:
            """字段类型兜底:模型可能把某字段返回成数组(如 title 给了 3 个候选)。"""
            if isinstance(v, str):
                return v
            if isinstance(v, list):
                return "\n".join(str(x) for x in v)
            return str(v or "")

        result = {k: _s(v) for k, v in result.items()}
        body = result.get("body", "")
        body_len = len(body)

        # 1. 字数(模板区间 ±20%)
        lo, hi = self._guess_len_range()
        if lo and hi:
            low, high = int(lo * 0.8), int(hi * 1.2)
            if not (low <= body_len <= high):
                issues.append(f"字数不达标: {body_len} 字,期望 {lo}~{hi}±20%")

        # 2. 结构齐全(关键节不能为空)
        missing = [s["key"] for s in self._sections() if not result.get(s["key"])]
        if missing:
            issues.append(f"缺少章节: {missing}")

        # 3. 敏感词(先摘掉"最优解/最佳实践"这类合法搭配,再查极限词)
        scanned = strip_ban_exempt(result.get("title", "") + body)
        hit_bans = [w for w in self.ban_words if w in scanned]
        if hit_bans:
            issues.append(f"命中违禁词: {hit_bans}")

        # 4. 事实性:正文必须含"据/报道/来源"标识或【待核实】标记,否则提示
        fact_notes: list[str] = []
        if body and not re.search(r"(据|报道|来源|待核实|公开信息|官方)", body):
            fact_notes.append("正文缺少来源标识,需人工核实关键事实")
        if "【待核实】" in body:
            fact_notes.append("正文含【待核实】标记,请人工补充")

        # 5. 反综述护栏:只对「必须表态」的结构生效(如 s3 论点驱动式)
        if self._must_take_stance() and self._guard_on():
            issues.extend(self._check_stance(result, body))

        return {"ok": len(issues) == 0, "issues": issues, "fact_notes": fact_notes}

    def _check_stance(self, result: dict, body: str) -> list[str]:
        """检查"是不是一篇有观点的分析",而不是综述。

        立场/转折/数字都按**全文**(导语 + 正文)判断 ——
        转折常常写在导语里,只扫正文会误杀(真实踩过)。
        """
        out: list[str] = []
        lead = str(result.get("lead") or "")
        full = lead + "\n" + body

        # 开头不许用信息播报腔
        head = full[:120]
        opened = [w for w in RECAP_OPENERS if w in head]
        if opened:
            out.append(f"开头是综述腔{opened}:分析文不要用「近日/据悉/随着」开场,要先用一个反常的细节或数字")

        # 必须表态
        if not any(m in full for m in STANCE_MARKERS):
            out.append("全文没有明确的立场表达:必须出现你的判断,不能只陈述事实")

        # 必须有转折(有反方才算分析)
        if not any(m in full for m in TURN_MARKERS):
            out.append("全文没有转折:必须写出主流看法或反方,并说明你为什么不同意")

        # 论据要有事实锚点
        if not re.search(r"\d", full):
            out.append("全文没有任何具体数字:论据需要具体事实支撑,不能全是形容词")

        # 论点本身要立得住
        thesis = str(result.get("thesis") or "").strip()
        if len(thesis) < 15:
            out.append(f"论点缺失或过短({len(thesis)} 字):需要 1~2 句明确判断,不能留空")
        else:
            empty = [w for w in EMPTY_CLAIMS if w in thesis]
            if empty:
                out.append(f"论点是空话{empty}:必须写成「可被反驳」的判断,而不是「值得关注」这类词")
        return out

    def _guess_len_range(self) -> tuple[int, int]:
        """正文(**全文**)的字数区间。

        取值优先级:
        1. body 段上的显式 `min_len`/`max_len` —— 模板作者最清楚,也最可靠;
        2. 描述里"像全文"的 `1200~1800 字`(排除「每条/每段」这类限定词后面的区间);
        3. 两个都没有 ⇒ `(0, 0)` = **这一次不判字数** ——
           宁可少一条护栏,也绝不拿一个错的数字把好稿子判死。

        ⚠️ 为什么要有「排除每条/每段」这一步(真实踩过,代价 = 3 次生成 + 熔断):
        s2 盘点式的 body 描述原文是「正文按条目盘点,**每条 150~250 字**」,
        旧实现直接正则抓第一个区间 → 把 150~250 当成**全文**区间(允许窗口 120~300),
        于是 1500 字的正文永远"字数不达标";更糟的是模型会照着这个数字越改越短
        (实测 1552 → 1267 → 914 字),最后触发熔断,用户看到的是"生成了半天最后失败"。
        """
        body_section = next(
            (s for s in self._sections() if s["key"] == "body"), None
        )
        if not body_section:
            return (0, 0)

        lo, hi = body_section.get("min_len"), body_section.get("max_len")
        if isinstance(lo, int) and isinstance(hi, int) and 0 < lo < hi:
            return lo, hi

        desc = str(body_section.get("desc") or "")
        for m in re.finditer(r"(\d{3,5})\s*[~-]\s*(\d{3,5})", desc):
            head = desc[max(0, m.start() - 6):m.start()]   # 区间前面那几个字,用来看它限定谁
            if any(hint in head for hint in PER_ITEM_HINTS):
                continue
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < a < b:
                return a, b
        return (0, 0)
