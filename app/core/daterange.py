"""时间范围参数:全站统一的口径(默认当天)。

为什么要统一:**"今天"到底怎么算,各页面必须一致** ——
否则看板说今日 57 条、文章库说今日 3 篇、分析报告又是另一批,用户没法对账。

约定:
- `today`(默认)= 本地时区的今天;
- `7d` / `30d` = **含今天的最近 N 天**(不是"往前推 N 天到昨天");
- `all` = 不限时间(看全部累积信息)。

日期一律按 `date(某列)` 比较,所以各表字段存的是日期串还是时间串都不影响。
"""
from __future__ import annotations

from datetime import date, timedelta

# 允许的取值;前端只认这几个
RANGES: tuple[str, ...] = ("today", "7d", "30d", "all")
DEFAULT_RANGE = "today"

RANGE_LABELS = {
    "today": "今天",
    "7d": "近 7 天",
    "30d": "近 30 天",
    "all": "全部时间",
}


def normalize_range(value: str | None) -> str:
    """把外部传入的值收敛到白名单内,非法值一律按默认(当天)处理。"""
    key = str(value or "").strip().lower()
    return key if key in RANGES else DEFAULT_RANGE


def range_bounds(value: str | None, today: date | None = None) -> tuple[str | None, str | None]:
    """返回闭区间的 (起始日, 结束日);`all` 返回 (None, None) 表示不加时间条件。"""
    key = normalize_range(value)
    if key == "all":
        return None, None
    end = today or date.today()
    days = {"today": 1, "7d": 7, "30d": 30}[key]
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def range_label(value: str | None) -> str:
    return RANGE_LABELS[normalize_range(value)]
