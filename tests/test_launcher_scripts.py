"""桌面启动链路的回归测试:`start_app.bat` -> `scripts/start_app.py`。

背景(为什么要做):
旧版 `start_app.bat` 把中文提示、`( ... )` 括号块、`^&^&` 全塞在一个 .bat 里,
结果 cmd.exe 在**解析阶段**就报

    `...` was unexpected at this time.

然后中止整个脚本 —— 用户只看到一句莫名其妙的英文,连一行提示都打不出来。
所以现在定下三条硬规矩,下面的测试就是防止它们被改回去:

1. `start_app.bat` 保持**纯 ASCII**(中文提示全部由 Python 打印);
2. 只用 `goto`,**不用括号块**、不用 `^&`;
3. 行尾必须是 CRLF(LF-only 的 .bat 会让 `goto` 找不到标签)。

另外顺带覆盖启动器的几个判据:端口探测、`/api/ping` 归属判断、`--check` 结论。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import start_app  # noqa: E402  (scripts/ 不是包,只能这样引)

BAT = ROOT / "start_app.bat"


def _desktop_launcher() -> Path | None:
    """找到这台机器上的桌面启动器;没有就返回 None。

    ⚠️ **不要把桌面绝对路径写死在仓库里** —— 那会把用户名/盘符随公开提交一起泄露,
    而且在别人的机器上必然跑不过。优先读环境变量,其次问 Windows 要真实桌面目录:
    有些机器的桌面被重定向到别的盘(实测本机就是),`~/Desktop` 并不可靠。

    需要在这台机器上跑这条断言时,先指定:
        set WECHAT_AGENT_DESKTOP_LAUNCHER=D:/Users/<you>/Desktop/WechatAgent.cmd
    """
    env = os.environ.get("WECHAT_AGENT_DESKTOP_LAUNCHER")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                return Path(buf.value) / "WechatAgent.cmd"
        except Exception:  # noqa: BLE001 — 拿不到就当没有,别让测试因此失败
            return None
    return None


# ------------------------------------------------------------ .bat 的三条规矩
def test_start_app_bat_exists():
    assert BAT.is_file(), "桌面启动器指向的就是这个文件"


def test_start_app_bat_is_pure_ascii():
    """⚠️ 中文别写回 .bat —— 见模块 docstring。中文提示归 Python 打。"""
    raw = BAT.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert bad == [], f"start_app.bat 里出现了非 ASCII 字节:{bad[:5]}"


def test_start_app_bat_avoids_parser_hazards():
    """"括号块 + ^&" 是 cmd 解析器最容易炸的组合,本文件一律不用。"""
    raw = BAT.read_bytes()
    for token in (b"(", b")", b"^&", b"&&", b"||"):
        assert token not in raw, f"start_app.bat 里不该出现 {token!r}(用 goto 分支代替)"


def test_start_app_bat_uses_crlf_line_endings():
    """LF-only 的批处理在 `goto` 标签上会翻车,必须 CRLF。"""
    raw = BAT.read_bytes()
    assert raw.count(b"\r\n") > 5
    assert raw.count(b"\n") == raw.count(b"\r\n"), "存在只有 LF 的行"


def test_start_app_bat_hands_off_to_python_launcher():
    text = BAT.read_text(encoding="ascii")
    assert "scripts\\start_app.py" in text
    assert "%~dp0" in text, "要先切到脚本所在目录,否则相对路径全错"
    assert "if errorlevel 1 goto" in text, "失败要停下来等用户看日志"


def test_desktop_launcher_points_to_start_app_bat():
    launcher = _desktop_launcher()
    if launcher is None or not launcher.is_file():
        pytest.skip("这台机器上没放桌面启动器(可用 WECHAT_AGENT_DESKTOP_LAUNCHER 指定)")
    raw = launcher.read_bytes()
    assert all(b < 128 for b in raw), "桌面启动器同样保持纯 ASCII"
    assert b"start_app.bat" in raw


# ------------------------------------------------------------ 启动器的判据
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int, body: bytes) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 别把测试输出刷满
            pass

    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_port_open_false_when_nobody_listens():
    assert start_app.port_open(_free_port()) is False


def test_ping_pong_recognises_our_own_service():
    """只有本项目的 /api/ping 才回 pong —— 不能"端口开着就当自己人"。"""
    port = _free_port()
    srv = _serve(port, b'{"pong": true}')
    try:
        assert start_app.ping_pong(port) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_ping_pong_false_for_foreign_service():
    port = _free_port()
    srv = _serve(port, b'{"hello": 1}')
    try:
        assert start_app.ping_pong(port) is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_check_says_ok_on_free_port():
    assert start_app.check(_free_port()) == 0


def test_check_flags_port_taken_by_someone_else():
    port = _free_port()
    srv = _serve(port, b'{"hello": 1}')
    try:
        assert start_app.check(port) == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_listener_pid_finds_the_serving_process():
    """netstat 解析:找到的确实是我们自己这个进程。"""
    port = _free_port()
    srv = _serve(port, b'{"pong": true}')
    try:
        assert start_app.listener_pid(port) is not None
    finally:
        srv.shutdown()
        srv.server_close()


def test_venv_python_points_into_the_project_venv():
    assert start_app.venv_python() == ROOT / ".venv" / "Scripts" / "python.exe"


def test_newest_source_mtime_is_meaningful():
    """用来判断"端口上跑的是不是改动前的旧进程",必须能拿到真实时间。"""
    assert start_app.newest_source_mtime() > 0
