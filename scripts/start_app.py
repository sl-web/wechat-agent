# -*- coding: utf-8 -*-
"""一键启动:环境自检 -> 起服务 -> 等就绪 -> 开浏览器。

    .venv\\Scripts\\python.exe scripts\\start_app.py            # 正常启动
    .venv\\Scripts\\python.exe scripts\\start_app.py --check    # 只自检,不起服务
    .venv\\Scripts\\python.exe scripts\\start_app.py --port 8099 --no-browser

由根目录 `start_app.bat`(桌面 `WechatAgent.cmd` 指向它)调用。

📌 为什么启动逻辑写在 Python、而不是 .bat 里:
    实测 .bat 里「中文 + 括号块 + `^&`」会让 cmd 解析器直接在**解析阶段**报
    ``... was unexpected at this time.`` 然后中止整个脚本 ——
    连一行提示都打不出来,用户只看到一个莫名其妙的错误。
    Python 里判断端口 / 进程 / HTTP 就绪都比批处理可靠,报错也能说人话。
    所以 `start_app.bat` 保持**纯 ASCII + 只用 goto、不用括号块**,
    只负责把控制权交过来;中文提示全部由本文件打印。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT / "wechat-agent-frontend" / "dist"
DIST_INDEX = DIST_DIR / "index.html"
APP_PKG = ROOT / "app"
DEFAULT_PORT = 8000
PING_PATH = "/api/ping"

_FILETIME_EPOCH = 11644473600  # 1601-01-01 -> 1970-01-01 的秒数


# ---------------------------------------------------------------- 控制台
def use_utf8_console() -> None:
    """让控制台按 UTF-8 输出中文。

    bat 里已有 `chcp 65001`,但脚本也可能被直接调用,所以自己再兜一次;
    顺便把 stdout/stderr 固定成 UTF-8 —— 否则中文按 cp936 编码时可能报
    UnicodeEncodeError(尤其重定向到文件时)。
    """
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def banner(port: int) -> None:
    print("=" * 50)
    print("  公众号文章 Agent  ·  光盛之路")
    print("-" * 50)
    print(f"  界面:http://localhost:{port}")
    print("  保持本窗口开启;关掉窗口 = 停止服务。")
    print("=" * 50)
    print()


# ---------------------------------------------------------------- 环境
def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def bootstrap_venv() -> Path | None:
    """首次运行:建 venv -> 装依赖 -> 装 Playwright 浏览器,然后用 venv 解释器重跑自己。

    返回 venv 解释器路径;失败返回 None(消息已经打给用户了)。
    """
    py = venv_python()
    venv_dir = ROOT / ".venv"
    print("[首次运行] 没找到虚拟环境 .venv,现在自动创建(第一次要几分钟,请耐心等)。")
    print(f"[首次运行] 使用的 Python:{sys.executable}")
    print()

    steps: list[tuple[str, list[str]]] = [
        ("创建虚拟环境", [sys.executable, "-m", "venv", str(venv_dir)]),
        ("升级 pip", [str(py), "-m", "pip", "install", "--upgrade", "pip"]),
        ("安装项目依赖", [str(py), "-m", "pip", "install", "-e", "."]),
        (
            "下载 Playwright 浏览器(推草稿 / 生成封面要用)",
            [str(py), "-m", "playwright", "install", "chromium"],
        ),
    ]
    for label, cmd in steps:
        print(f"[首次运行] {label} ...")
        try:
            rc = subprocess.call(cmd, cwd=str(ROOT))
        except OSError as exc:
            print(f"[错误] {label} 起不来:{exc}")
            return None
        if rc != 0:
            print()
            print(f"[错误] {label} 失败(退出码 {rc})。上面的报错可以直接发给小墨。")
            return None

    print()
    print("[首次运行] 环境准备完毕,继续启动。")
    print()
    # 换成 venv 的解释器重跑自己(同一个窗口、同一份 argv),之后就不会再走这条路了
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])
    return py  # pragma: no cover - execv 不会返回


def check_frontend() -> bool:
    """前端构建产物是否就绪。缺了也能起后端,但浏览器里只有 API、没有界面。"""
    if DIST_INDEX.is_file():
        print(f"[自检] 前端构建产物:已就绪({DIST_INDEX.relative_to(ROOT)})")
        return True
    print("[提示] 前端还没构建,浏览器里的界面会打不开(只有接口能访问)。")
    print("       请在 wechat-agent-frontend 目录执行: npm install 然后 npm run build")
    print()
    return False


# ---------------------------------------------------------------- 端口
def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def ping_pong(port: int) -> bool:
    """该端口上跑的是不是「我们的」服务 —— 只有本项目的 /api/ping 才回 pong。

    不用"端口开着就当成我们的" —— 那样任何占着 8000 的程序都会被误认。
    """
    url = f"http://127.0.0.1:{port}{PING_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            return resp.status == 200 and b"pong" in resp.read(200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def listener_pid(port: int) -> int | None:
    """从 netstat 里取正在 LISTEN 该端口的进程号;取不到返回 None。"""
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    pat = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", re.IGNORECASE)
    for line in (proc.stdout or "").splitlines():
        found = pat.search(line)
        if found:
            return int(found.group(1))
    return None


def _process_start_ts(pid: int) -> float | None:
    """取进程启动时间(Unix 秒)。取不到就返回 None —— 这只是个提醒,不能因此拦住启动。"""
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        creation, exited, kernel, user = (FILETIME() for _ in range(4))
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ticks / 1e7 - _FILETIME_EPOCH
    finally:
        kernel32.CloseHandle(handle)


def newest_source_mtime() -> float:
    """后端代码 / 启动脚本 / 前端构建产物里最新的修改时间。"""
    newest = 0.0
    targets: list[Path] = list(APP_PKG.rglob("*.py"))
    targets += list((ROOT / "scripts").glob("*.py"))
    targets += list(DIST_DIR.glob("*"))
    for path in targets:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def stale_hint(port: int) -> None:
    """端口上跑着我们的服务时,顺便判断它是不是「旧进程」。

    这是本项目反复踩的坑:改了后端代码但没重启 -> 新接口 404,前端弹 Not Found,
    很容易误判成"接口没实现"。判据:服务进程的启动时间早于最新代码的修改时间。
    """
    pid = listener_pid(port)
    if pid is None:
        print(f"[提示] 无法确定 {port} 端口上是哪个进程(请自行确认它的窗口)。")
        return
    started = _process_start_ts(pid)
    newest = newest_source_mtime()
    if started is None or newest <= 0:
        print(f"[提示] {port} 端口上的服务进程号 {pid},请自行确认它是不是你开着的那个窗口。")
        return
    if started < newest:
        print(f"[注意] 该服务(PID {pid})启动时间早于最新代码 —— 很可能是**改动之前**的旧进程,")
        print("       新接口会返回 404。想用新代码请先关掉它,再重新运行本启动器。")
    else:
        print(f"[提示] 该服务(PID {pid})比代码新,可以直接用。")


# ---------------------------------------------------------------- 启动
def open_browser_when_ready(port: int) -> None:
    """后台等 /api/ping 就绪再开浏览器。

    为什么不在起服务之前直接 open:那时服务还没起来,用户看到的是一张
    "无法连接" 的空白页,体验比不自动打开更糟。
    """
    cmd = [sys.executable, str(ROOT / "scripts" / "open_app.py"), "--port", str(port)]
    try:
        subprocess.Popen(cmd, cwd=str(ROOT))
    except OSError as exc:  # 开不了浏览器只是少个便利,不该拦住服务
        print(f"[提示] 后台上开浏览器的助手没起来({exc}),请手动打开 http://localhost:{port}")


def run_server(port: int) -> int:
    try:
        import uvicorn
    except ImportError:
        print("[错误] 缺少 uvicorn(依赖没装全)。请执行:")
        print(f'       "{venv_python()}" -m pip install -e .')
        return 1
    print(f"[服务] 启动中……(地址 http://localhost:{port};本窗口就是服务本身)")
    print()
    try:
        uvicorn.run("app.main:app", port=port, log_level="info")
    except KeyboardInterrupt:
        pass
    print()
    print("服务已停止,本窗口可以关掉了。")
    return 0


def check(port: int) -> int:
    """只自检,不起服务。给排障和自动化验证用。"""
    print(f"[自检] 运行在:{sys.executable}")
    check_frontend()
    if not port_open(port):
        print(f"[自检] 端口 {port}:空闲,可以启动")
        print("[自检] 结论:可以启动。")
        return 0
    if ping_pong(port):
        print(f"[自检] 端口 {port}:已有本服务在运行")
        stale_hint(port)
        print("[自检] 结论:无需再启动;直接访问界面即可。")
        return 0
    print(f"[自检] 端口 {port}:被**别的**程序占用(不是本服务)")
    pid = listener_pid(port)
    if pid:
        print(f"       占用进程号:{pid}")
    print("[自检] 结论:先腾出该端口再启动(或换个端口,见 --port)。")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公众号文章 Agent 一键启动")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true", help="只自检,不启动服务")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    use_utf8_console()
    os.chdir(ROOT)  # app.main 里有相对路径(data/app.db),必须站在项目根上

    if args.check:
        return check(args.port)

    banner(args.port)
    if not venv_python().exists():
        if bootstrap_venv() is None:
            return 1
    if sys.executable != str(venv_python()):
        # 用别的解释器跑进来的(比如系统 python):换成 venv 的那个,依赖才齐
        venv = venv_python()
        if venv.exists():
            print(f"[启动] 切换到虚拟环境解释器:{venv}")
            print()
            os.execv(str(venv), [str(venv), str(Path(__file__).resolve()), *sys.argv[1:]])

    check_frontend()

    if port_open(args.port):
        if ping_pong(args.port):
            print(f"[提示] {args.port} 端口已有本服务在运行,直接打开浏览器。")
            stale_hint(args.port)
            print()
            if not args.no_browser:
                webbrowser.open(f"http://localhost:{args.port}")
            return 0
        print(f"[错误] {args.port} 端口被别的程序占用了,本服务起不来。")
        pid = listener_pid(args.port)
        if pid:
            print(f"       占用进程号:{pid} —— 可在任务管理器里按 PID 结束它,或用 --port 换端口。")
        return 1

    if not args.no_browser:
        open_browser_when_ready(args.port)
    return run_server(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
