"""熔断器与黑名单测试。"""
from __future__ import annotations

from app.core.circuit_breaker import CircuitBreaker, HotspotBlacklist


def test_circuit_breaker_trips_after_limit():
    cb = CircuitBreaker(name="generate", limit=3)
    assert cb.record_failure() is True   # 1/3
    assert cb.record_failure() is True   # 2/3
    assert cb.record_failure() is False  # 3/3 → 熔断打开
    assert cb.is_open is True


def test_circuit_breaker_reset():
    cb = CircuitBreaker(name="push", limit=2)
    cb.record_failure()
    cb.record_failure()  # 打开
    assert cb.is_open
    cb.reset()
    assert not cb.is_open
    assert cb.failures == 0


def test_hotspot_blacklist_after_threshold():
    bl = HotspotBlacklist(threshold=5)
    for _ in range(4):
        assert bl.record_failure(101) is True
    assert bl.record_failure(101) is False  # 第 5 次拉黑
    assert bl.is_blacklisted(101)


def test_hotspot_blacklist_per_topic():
    bl = HotspotBlacklist(threshold=3)
    bl.record_failure(1)
    bl.record_failure(1)
    bl.record_failure(2)
    assert bl.is_blacklisted(1) is False  # 1 号 2 次未达阈值
    bl.record_failure(1)                  # 1 号达 3 次
    assert bl.is_blacklisted(1)
    assert bl.is_blacklisted(2) is False  # 2 号不受影响
