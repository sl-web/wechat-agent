"""熔断器:重试上限 + 规则兜底 + 热点黑名单,防无限重复(PRD 5.3.3)。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class CircuitBreaker:
    """单任务熔断器。

    - 单次重试超过 limit 次 → 熔断打开,record() 返回 False(应停止重试走兜底)
    - reset() 成功后闭合
    """

    name: str
    limit: int = 3
    _failures: int = field(default=0, init=False)
    _open: bool = field(default=False, init=False)
    _tripped_at: float | None = field(default=None, init=False)

    def record_failure(self) -> bool:
        """记录一次失败。返回 True=仍可重试;False=已达上限,触发熔断。"""
        self._failures += 1
        if self._failures >= self.limit:
            self._open = True
            self._tripped_at = time.time()
            return False
        return True

    def reset(self) -> None:
        self._failures = 0
        self._open = False
        self._tripped_at = None

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def failures(self) -> int:
        return self._failures


class HotspotBlacklist:
    """热点黑名单:同一热点累计失败达到阈值则当日不再尝试。"""

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self._counter: dict[int, int] = {}
        self._date: str = ""

    def _roll_date(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._counter.clear()

    def record_failure(self, topic_id: int) -> bool:
        """记录一次失败。返回 False 表示已拉黑。"""
        self._roll_date()
        self._counter[topic_id] = self._counter.get(topic_id, 0) + 1
        return self._counter[topic_id] < self.threshold

    def is_blacklisted(self, topic_id: int) -> bool:
        self._roll_date()
        return self._counter.get(topic_id, 0) >= self.threshold


def new_run_id() -> str:
    """生成一次完整流程的 run_id。"""
    return f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
