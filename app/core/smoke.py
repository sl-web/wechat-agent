"""S0 冒烟验证:建库 → 配置读写 → 熔断 → FastAPI 启动前自检。

运行: .venv/Scripts/python -m app.core.smoke
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from app.core import config as cfg
from app.core.circuit_breaker import CircuitBreaker, HotspotBlacklist
from app.core.db import connect, execute, init_db, query

TABLES = [
    "hot_topics", "articles", "article_stats", "templates",
    "configs", "schedules", "run_logs", "kb_clean_logs",
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "smoke.db"
    init_db(tmp)
    conn = connect(tmp)
    try:
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        missing = set(TABLES) - names
        assert not missing, f"缺表: {missing}"
        print(f"[OK] 8 张核心表已创建: {TABLES}")

        cfg.set_config("top_n", 42, db=conn)
        assert cfg.get_config("top_n", conn) == 42
        print("[OK] 配置写入/读取覆盖正常 (top_n=42)")

        cb = CircuitBreaker(name="smoke", limit=2)
        assert cb.record_failure()
        assert not cb.record_failure()
        assert cb.is_open
        print("[OK] 熔断器: 达上限后正确打开")

        bl = HotspotBlacklist(threshold=2)
        bl.record_failure(1)
        assert not bl.record_failure(1)
        print("[OK] 热点黑名单: 达阈值后拉黑")
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)

    print("\nS0 冒烟验证全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
