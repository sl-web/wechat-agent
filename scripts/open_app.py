# -*- coding: utf-8 -*-
"""等后端就绪后打开浏览器。

为什么单独一个脚本、而不是写在 .bat 里:
- 批处理里做"轮询端口 + 延时"要靠 `ping` 骗时间,还容易掉进引号嵌套的坑;
- 这里能**真的等到服务可用**再开浏览器 —— 否则用户看到的是"无法连接"的空白页,
  还得自己刷新,那种体验比不自动打开更糟。

由 `scripts/start_app.py` 在后台拉起(前台那个窗口留给服务打日志)。
用 `--dry-run` 可以只验证等待逻辑、不开浏览器。
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
import webbrowser

PING_PATH = "/api/ping"


def wait_until_ready(port: int, timeout: float = 60.0, interval: float = 0.4) -> bool:
    """轮询 /api/ping 直到 200 或超时。返回是否就绪。"""
    url = f"http://127.0.0.1:{port}{PING_PATH}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(interval)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="等后端就绪后打开浏览器")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true", help="只等就绪,不打开浏览器")
    args = ap.parse_args(argv)

    # 用 localhost 而不是 127.0.0.1:Vite 时代两者有差异,统一成用户看得懂的地址
    url = f"http://localhost:{args.port}"
    ok = wait_until_ready(args.port, timeout=args.timeout)
    if not ok:
        print(f"[启动器] 等了 {args.timeout:.0f} 秒服务仍未就绪,请查看上方窗口的日志。", file=sys.stderr)
        return 1
    if args.dry_run:
        print(f"[启动器] 服务已就绪(未打开浏览器):{url}")
        return 0
    print(f"[启动器] 服务已就绪,正在打开 {url}")
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
