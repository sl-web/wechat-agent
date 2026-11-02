"""发布时段建议(读者活跃时段)。

### 为什么要它
公众号文章的阅读**集中在推送后的前几小时** —— 推送时正好在看手机的读者才会打开。
所以"什么时候发"和"写什么"同样影响打开率,而这一步以前完全没有。

### ⚠️ 默认值是经验值,不是从你的数据算出来的
`publish_window` 默认给的是通用经验时段(早通勤 / 午休 / 晚间)。
真正属于你这个号的时段在后台「数据分析 → 内容分析」里,可以照着改这个配置。
把它做成可配置而不是写死,就是为了让数据说了话之后能改。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from app.core.config import get_config

DEFAULT_WINDOW = "07:00-09:00, 12:00-13:00, 20:00-22:00"

_PAT = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


def parse_window(spec: str | None = None) -> list[tuple[int, int]]:
    """把 "07:00-09:00, 20:00-22:00" 解析成 [(开始分钟, 结束分钟)]。

    非法片段**直接跳过**(而不是整体报错)—— 配置写错一个字不该让功能整体不可用。
    """
    raw = spec if spec is not None else str(get_config("publish_window") or DEFAULT_WINDOW)
    out: list[tuple[int, int]] = []
    for part in str(raw or "").split(","):
        m = _PAT.match(part.strip())
        if not m:
            continue
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if 0 <= start < 1440 and 0 < end <= 1440 and end > start:
            out.append((start, end))
    return sorted(out)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _label(seg: tuple[int, int]) -> str:
    return f"{_hhmm(seg[0])}-{_hhmm(seg[1])}"


def status(spec: str | None = None, now: datetime | None = None) -> dict:
    """当前是否处于推荐时段,以及下一个时段是什么时候。"""
    windows = parse_window(spec)
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute

    if not windows:
        return {"in_window": False, "windows": [], "now": _hhmm(cur),
                "next": None, "hint": "publish_window 配置为空或格式不对,已停用时段建议"}

    for s, e in windows:
        if s <= cur < e:
            return {"in_window": True, "windows": [_label(w) for w in windows],
                    "now": _hhmm(cur), "current": _label((s, e)),
                    "minutes_left": e - cur, "next": None,
                    "hint": f"现在正在推荐时段 {_label((s, e))} 内,适合推送"}

    nxt = next((w for w in windows if w[0] > cur), None)
    if nxt is None:                       # 今天的时段都过了 → 明天第一个
        nxt = windows[0]
        delta = (1440 - cur) + nxt[0]
    else:
        delta = nxt[0] - cur
    at = (now + timedelta(minutes=delta)).strftime("%H:%M")
    return {"in_window": False, "windows": [_label(w) for w in windows],
            "now": _hhmm(cur), "current": None, "minutes_left": 0,
            "next": {"window": _label(nxt), "at": at, "minutes_until": delta},
            "hint": f"下一个推荐时段是 {_label(nxt)}(约 {at}),建议那时再推送"}
