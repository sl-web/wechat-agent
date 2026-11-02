"""把「回流数据」接回「选题」——从账号自己的历史表现里学出「什么样的选题会被转发」。

### 为什么需要它
选题评分原本只有三项:平台热度 / 时效衰减 / 来源权重 ——
**三条全是"这条新闻有多热",没有一条是"我的读者会不会转"**。
而实测数据说明:分享是公众号唯一的放大器(约 **1 次分享 ≈ 15~20 阅读**,
而微信推荐流量平均只有 1.2),所以"会不会被转发"必须成为选题的一个维度。

### 数据从哪来
不需要新数据源 —— `mp_api.list_published()` 已经能拿到**标题 + 阅读/分享**。
这就是账号自己做过的实验记录:哪类标题被转得多,数据里自己会说话。

### ⚠️ 三件必须说清楚的事
1. **样本量只够给弱信号**。几十篇的量级做不出可靠模型,所以它只作为热度上的
   一个**有界系数**(默认 ±35%),绝不覆盖热度与时效 —— 宁可影响小,
   也不能让一个弱统计把当天的选题带偏。
2. **无画像时返回中性值 0.5**,等价于"不启用",不会改变原有排序。
3. **学的是"形态",不是"领域"**。形态特征(疑问式/数字/第二人称…)与赛道无关,
   换内容方向也不用重训;高频词则是从标题里统计出来的,会随账号自己变化。
"""
from __future__ import annotations

import re
import statistics

# 画像存在 configs 表里(JSON),不额外建表
PROFILE_KEY = "growth_profile"

# 标题「形态」特征 —— 刻意只用**与领域无关**的形态,避免把某个赛道写死。
_FORM_PATTERNS: tuple[tuple[str, str, re.Pattern], ...] = (
    # ⚠️ 不能只匹配 `？` —— 实测「2B模型超越GPT-5？算力账单不认跑分」会被误判成"提问式"
    # (它 0 分享)。要**含疑问词**或**以问号收尾**才算。
    ("question", "疑问/提问式", re.compile(r"为什么|怎么|如何|是什么|多少|[?？]$|吗[?？]?$|呢[?？]?$")),
    ("number", "含具体数字", re.compile(r"\d")),
    ("second_person", "第二人称/代入感", re.compile(r"你|我们|自己|每个人|普通人")),
    ("exclaim", "感叹/强情绪", re.compile(r"[!！]")),
    ("reveal", "破折号揭底", re.compile(r"——|--")),
    # 大众认知锚点。当前账号这类标题只有 2 篇(< 最小出现次数),暂时不参与打分 ——
    # 留着是为了数据涨起来后自动生效,不用再改代码。
    ("public_figure", "公众人物/机构锚点",
     re.compile(r"总统|首相|总理|部长|CEO|创始人|董事长|院士|教授|首富")),
)
_SHORT_TITLE_MAX = 18

# 停用词:高频但无区分度,留着只会给所有标题加一样的分
_STOP = {
    "的", "了", "是", "在", "和", "与", "就", "都", "而", "也", "很", "又", "才",
    "你", "我", "他", "她", "它", "这", "那", "们", "一个", "我们", "他们",
    "什么", "怎么", "为什么", "如何", "不是", "没有", "可以", "自己", "时候",
    "因为", "所以", "如果", "就是", "一直", "已经", "还是", "这样", "那样",
}
_MIN_TERM_LEN, _MAX_TERM_LEN = 2, 4
# 结构性虚词:任何含它们的 n-gram 都不是一个"词",只是跨词边界切出来的碎片。
# 真实踩过:从「别人的因果」切出 `人的` / `人的因`,lift 高得离谱却毫无意义。
# ⚠️ 这里**只能放单字** —— 曾把多字词「如果」塞进来,于是单字 `果` 也变成虚词,
#    连「因果」一起误杀。也不放 为/不/最/更 等字:它们能出现在「行为」「不断」这类实义词里。
_PARTICLES = set("的了是在和与也就都而很又才们之其此把被对从向呢吗吧啊着过"
                 "什么怎我你他她它这那")
_MIN_SAMPLES = 8          # 少于这个条数就不学(学了也不可信)
_MIN_OCCUR = 3            # 一个形态/词至少出现在 3 篇里才采信
_MAX_TERMS = 15
# 长度只作为**画像展示**,不参与打分:它是"修饰"不是"钩子",
# 而且会把「疑问式的长标题」这类好标题一起压下去(实测踩过)。
_LENGTH_FORMS = ("short", "long")


def _forms(title: str) -> list[str]:
    """标题命中的形态 key。短/长标题也算一种形态。"""
    out = [key for key, _label, pat in _FORM_PATTERNS if pat.search(title)]
    out.append("short" if len(title) <= _SHORT_TITLE_MAX else "long")
    return out


def _terms(title: str) -> set[str]:
    """粗糙但够用的中文词提取:切出 2~4 字连续片段,过滤停用词与虚词碎片。

    没有引入分词依赖 —— 标题很短,配合"最小出现次数 + lift 阈值 + 子串去重 + 虚词过滤",
    噪声可以接受(而且它本来就只占很小的权重)。
    """
    zh = re.sub(r"[^\u4e00-\u9fff]+", " ", title)
    out: set[str] = set()
    for seg in zh.split():
        for n in range(_MIN_TERM_LEN, _MAX_TERM_LEN + 1):
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                if w in _STOP or any(c in _PARTICLES for c in w):
                    continue
                out.add(w)
    return out


def _rows(items: list[dict]) -> list[dict]:
    """把发表记录拍平成 {title, read, share, rate}。

    用 **转发率(share / read)** 而不是分享绝对数做目标 ——
    否则"读的人多所以转得多"会把"内容值得转"混淆掉。
    """
    out = []
    for it in items or []:
        st = it.get("stats") or {}
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        read = int(st.get("read_count") or 0)
        share = int(st.get("share_count") or 0)
        out.append({"title": title, "read": read, "share": share,
                    "rate": (share / read) if read > 0 else 0.0})
    return out


def build_profile(items: list[dict]) -> dict:
    """从发表记录学出传播画像。样本不足时返回 ok=False 的空画像。"""
    rows = _rows(items)
    if len(rows) < _MIN_SAMPLES:
        return {"ok": False, "samples": len(rows),
                "reason": f"发表记录只有 {len(rows)} 条,不足以学习(至少 {_MIN_SAMPLES} 条)"}

    # 基线:全站平均转发率。低于 0.01 时抬到 0.01,避免除零与 lift 爆炸。
    base = statistics.mean(r["rate"] for r in rows)
    base = max(base, 0.01)

    # ---- 形态 lift ----
    forms = []
    total = len(rows)
    for key, label, _pat in _FORM_PATTERNS:
        hit = [r for r in rows if key in _forms(r["title"])]
        if len(hit) < _MIN_OCCUR:
            continue
        forms.append({"key": key, "label": label, "n": len(hit),
                      "lift": round(statistics.mean(r["rate"] for r in hit) / base, 3)})
    for key, label, cond in (("short", f"短标题(≤{_SHORT_TITLE_MAX}字)", lambda t: len(t) <= _SHORT_TITLE_MAX),
                             ("long", f"长标题(>{_SHORT_TITLE_MAX}字)", lambda t: len(t) > _SHORT_TITLE_MAX)):
        hit = [r for r in rows if cond(r["title"])]
        if len(hit) >= _MIN_OCCUR:
            forms.append({"key": key, "label": label, "n": len(hit),
                          "lift": round(statistics.mean(r["rate"] for r in hit) / base, 3)})

    # ---- 高频词 lift ----
    bucket: dict[str, list[float]] = {}
    for r in rows:
        for w in _terms(r["title"]):
            bucket.setdefault(w, []).append(r["rate"])
    cands = [
        {"term": w, "n": len(v), "lift": statistics.mean(v) / base}
        for w, v in bucket.items() if len(v) >= _MIN_OCCUR and len(w) >= 2
    ]
    cands.sort(key=lambda x: (-x["lift"], -len(x["term"]), x["term"]))
    kept: list[dict] = []
    for c in cands:
        if c["lift"] < 1.2:                       # 不高于基线 20% 的词没有信息量
            continue
        # 子串去重:保留更长的那个(「为什么」比「为什」有意义)
        if any(c["term"] in k["term"] or k["term"] in c["term"] for k in kept):
            continue
        kept.append({"term": c["term"], "n": c["n"], "lift": round(c["lift"], 3)})
        if len(kept) >= _MAX_TERMS:
            break

    forms.sort(key=lambda x: -x["lift"])
    return {
        "ok": True,
        "samples": total,
        "baseline_rate": round(base, 4),
        "average_read": round(statistics.mean(r["read"] for r in rows), 1),
        "average_share": round(statistics.mean(r["share"] for r in rows), 2),
        "forms": forms,
        "terms": kept,
        "built_at": None,          # 由调用方(保存时)填时间戳
        "hint": "lift = 该类标题的平均转发率 ÷ 全站平均转发率;>1 表示更容易被转发",
        "limits": (
            "这只识别**标题形态与高频词**,看不懂语义 —— "
            "比如「美国总统拜登…」这类靠公众人物传播的标题,它给不出加分。"
            "样本量只够给弱信号,所以它只作为热度的有界系数(默认 ±35%)。"
        ),
    }


def load_profile(db=None) -> dict:
    from app.core.config import get_config

    v = get_config(PROFILE_KEY, db)
    return v if isinstance(v, dict) else {}


def save_profile(profile: dict) -> None:
    from app.core.config import set_config

    set_config(PROFILE_KEY, profile, updated_by="growth")


def refresh_profile(db_path: str | None = None) -> dict:
    """从公众号发表记录重建画像并落库。只读后台。"""
    import time

    from app.pusher.mp_api import list_published

    items = list_published().get("items") or []
    prof = build_profile(items)
    prof["built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if prof.get("ok"):
        save_profile(prof)
    return prof


# |lift-1| 小于这个数就当成噪声。尺度由样本量决定:几十篇的样本分不出 5% 的差异。
_NOISE = 0.15


def _lift_to_score(lift: float) -> float:
    """lift → 0~1:1.0(与平均持平)= 0.5,+0.15 = 1.0,-0.15 = 0.0。"""
    d = max(-1.0, min(1.0, (lift - 1.0) / _NOISE))
    return round(0.5 + 0.5 * d, 4)


def growth_score(title: str, profile: dict | None = None) -> float:
    """传播潜力 0~1;**0.5 = 与账号历史平均持平**,无画像时恒为 0.5(中性,不改变排序)。

    ⚠️ **只看标题,不看摘要**。画像本来就是从标题学出来的;拿学到的词去匹配摘要
    会产生大量误判 —— 实测「学会」在科技摘要里是"学会使用",和人生类标题里的
    语境完全不同,结果 100 条成员里 14 条被错误加分。精度优先于召回。

    ⚠️ **只取正向钩子加分,不用弱负向扣分**。理由:
    负向信号在这点样本量下不可靠 —— 一个 lift=0.81 的形态只来自 7 篇,
    拿它去扣分,会把本来会被转发的标题打下去。这和项目里
    「一条会误伤的护栏,代价比它挡下的风险还大」是同一个教训。
    所以:没有命中正向钩子 → 中性 0.5,**不扣分**。

    只统计**钩子型**形态(疑问/感叹/第二人称/破折号)与学到的词;
    标题长短只在画像里展示、不参与打分(见 `_LENGTH_FORMS` 的说明)。
    """
    if not profile or not profile.get("ok"):
        return 0.5

    text = title or ""
    lifts: list[float] = []
    by_key = {f["key"]: f for f in (profile.get("forms") or [])
              if f.get("n", 0) >= _MIN_OCCUR and f.get("key") not in _LENGTH_FORMS}
    for key in _forms(text):
        if key in _LENGTH_FORMS:
            continue
        f = by_key.get(key)
        if f:
            lifts.append(float(f.get("lift") or 1.0))

    for t in profile.get("terms") or []:
        term = str(t.get("term") or "")
        if term and term in text:
            lifts.append(float(t.get("lift") or 1.0))

    pos = [x for x in lifts if x > 1.0]
    if not pos:
        return 0.5
    # 一个强钩子就够了,不用平均(平均会被同时命中的中性特征稀释掉)
    return _lift_to_score(max(pos))
