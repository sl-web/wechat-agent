"""文章类型建议:判断这组素材更适合哪种论证姿态。

四种类型与 `prompt.ARGUMENT_MODES` 一一对应::

    industry_judgment  行业判断型  —— 这件事说明什么趋势,依据是什么
    critical           批判质疑型  —— 挑战主流叙事,指出它站不住脚的地方
    teardown           深度拆解型  —— 拆开看真正的运转逻辑(钱从哪来、卡在哪)
    forecast           趋势预测型  —— 给出可被事后检验的预测

### 为什么先用规则而不是直接调 LLM
分析阶段本来**不调 LLM**(清洗 / 评分 / 聚类都是本地计算,1 秒内完成)。
在这里插一次 LLM 会让"分析"变成几十秒,而且每跑一次都花钱。

所以分两层:
1. **分析阶段用规则**(本模块):零成本、即时,给一个"可以据以选择"的建议,
   并把**判断依据**(命中了哪些特征词)一起给出,用户能判断可不可信;
2. **用户勾选素材后由 LLM 复核**:跨主题多选时规则无能为力 ——
   两组素材各自的信号可能互相冲突,只有模型能综合判断。
"""
from __future__ import annotations

TYPE_LABELS = {
    "industry_judgment": "行业判断型",
    "critical": "批判质疑型",
    "teardown": "深度拆解型",
    "forecast": "趋势预测型",
}
DEFAULT_TYPE = "industry_judgment"

# 分数表始终包含四种类型(含兜底的行业判断型),便于前端/日志看到全貌
_ZERO_SCORES = {t: 0 for t in TYPE_LABELS}

# 每种类型的文本特征词。命中越多说明语料越像这种论证姿态。
#
# ⚠️ 这里**只放有区分度的信号**(矛盾冲突 / 供应链成本 / 时间表)。
# 「发布」「上线」「宣布」这类中性播报词**故意不收** —— 它们在科技新闻里无处不在,
# 一旦计入,所有主题都会被推成"行业判断型",推荐就等于没有。
# 行业判断型改为"其它三种都没明显信号时"的兜底默认。
_SIGNALS: dict[str, tuple[str, ...]] = {
    "critical": (
        "质疑", "争议", "回应", "辟谣", "反驳", "存疑", "夸大", "翻车", "打脸",
        "投诉", "起诉", "违规", "下架", "不满", "批评", "泡沫", "炒作", "噱头",
        "割韭菜", "智商税", "名不副实", "泼冷水", "站不住", "隐瞒", "对峙",
    ),
    "teardown": (
        "成本", "供应链", "毛利", "财报", "定价", "计费", "拆解", "良率", "代工",
        "晶圆", "产能", "库存", "现金流", "账期", "渠道", "分成", "算力", "电费",
        "能耗", "带宽", "折旧", "交付", "认证", "补贴", "配额", "单价", "周转",
    ),
    "forecast": (
        "预计", "预期", "计划", "明年", "未来", "路线图", "展望", "即将", "有望",
        "拐点", "窗口期", "规划", "试点", "量产", "时间表", "目标", "下半年",
        "下一个", "下一步", "路线", "长期看",
    ),
}

# 单个特征词最多计 2 次,避免一个高频词把整段语料带偏
_MAX_PER_WORD = 2
# 推荐时用于分析的文本上限(字符)
_TEXT_LIMIT = 1400
# 有区分度的信号低于这个分数,就认为语料没有明显取向,退回最通用的类型
_MIN_DISCRIMINATING = 2


def suggest_type(topic: str, members: list[dict] | None = None) -> dict:
    """按主题与其成员报道的文本特征推荐文章类型。

    返回 `{type, label, reason, scores, confidence}`;
    `reason` 会写明命中了哪些特征词 —— 让用户知道**为什么**推荐它,而不是黑箱。
    """
    parts = [topic or ""]
    for m in members or []:
        parts.append(f"{m.get('title') or ''} {m.get('summary') or ''}")
    text = " ".join(parts)[:_TEXT_LIMIT]

    scores: dict[str, int] = {t: v for t, v in _ZERO_SCORES.items()}
    hits: dict[str, list[str]] = {t: [] for t in TYPE_LABELS}
    for t, words in _SIGNALS.items():
        for w in words:
            n = text.count(w)
            if not n:
                continue
            scores[t] += min(n, _MAX_PER_WORD)
            if len(hits[t]) < 3:
                hits[t].append(w)

    best = max(scores, key=lambda t: scores[t])
    top = scores[best]
    if top < _MIN_DISCRIMINATING:       # 三种有区分度的信号都不明显 → 用最通用的类型
        return {
            "type": DEFAULT_TYPE,
            "label": TYPE_LABELS[DEFAULT_TYPE],
            "reason": "语料以事实播报为主,没有明显倾向,建议用最通用的行业判断型",
            "scores": scores,
            "confidence": "low",
        }

    second = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
    if top >= 3 and top >= second * 2:
        conf = "high"
    elif top >= second:
        conf = "medium"
    else:
        conf = "low"
    reason = f"语料中出现「{'、'.join(hits[best])}」等特征,更接近{TYPE_LABELS[best]}的写法"
    return {
        "type": best,
        "label": TYPE_LABELS[best],
        "reason": reason,
        "scores": scores,
        "confidence": conf,
    }
