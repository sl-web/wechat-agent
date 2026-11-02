"""推送编排:扫码登录会话管理 + 文章→草稿的后台推送任务。

Playwright 是重资源且**不能并发**,因此:
- 进程内串行锁:同一时刻只允许一个浏览器实例
- 长任务放后台线程,前端轮询 `GET /api/pusher/status` 看进度(复用进度面板的思路)
- 失败一律给出 error(发生了什么)+ hint(该怎么办),并支持降级到方案 B

状态变更的唯一出口是 `review.service.push_to_drafts()`:
本模块只负责「把稿子真的推进公众号」,推进成功后回调它落库。
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import get_all_config, get_config, set_config
from app.core.db import connect
from app.core.logger import get_logger
from app.core.settings import settings
from app.pusher.client import session_exists, state_path

logger = get_logger("pusher.service")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 串行锁:浏览器实例不能并发(同时开两个会被风控,也会互相抢页面)
_job_lock = threading.Lock()
_last_push_ts: float = 0.0

SESSION_VERIFIED_KEY = "mp_session_verified_at"

_state: dict = {
    "login": {
        "running": False, "phase": "idle", "message": "",
        "started_at": None, "finished_at": None, "ok": None,
        "error": None, "hint": None,
    },
    "push": {
        "running": False, "article_id": None, "phase": "idle", "message": "",
        "steps": [], "logs": [], "started_at": None, "finished_at": None,
        "elapsed": 0.0, "ok": None, "error": None, "hint": None, "result": None,
    },
    # 封面生成:与 push 同构,但通常几秒就结束,前端只在按钮上转圈
    "cover": {
        "running": False, "article_id": None, "phase": "idle", "message": "",
        "steps": [], "logs": [], "started_at": None, "finished_at": None,
        "elapsed": 0.0, "ok": None, "error": None, "hint": None, "result": None,
    },
}
_started: dict[str, float | None] = {"login": None, "push": None, "cover": None}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- 状态读写 ----------------

def _mark(kind: str, phase: str, message: str = "") -> None:
    with _lock:
        st = _state[kind]
        st["phase"] = phase
        st["message"] = message
        logs = st.setdefault("logs", [])
        logs.append({"t": _now(), "phase": phase, "message": message})
        st["logs"] = logs[-60:]


def _step(kind: str, key: str, label: str, status: str, detail: str = "") -> None:
    with _lock:
        st = _state[kind]
        for s in st["steps"]:
            if s["key"] == key:
                s["status"] = status
                s["detail"] = detail
                break
        else:
            st["steps"].append({"key": key, "label": label, "status": status, "detail": detail})


def _finish(kind: str, ok: bool, error: str | None = None,
            hint: str | None = None, result: dict | None = None) -> None:
    with _lock:
        st = _state[kind]
        st.update(
            running=False, finished_at=_now(), ok=ok,
            error=error, hint=hint, result=result,
        )
        if _started.get(kind):
            st["elapsed"] = round(time.time() - _started[kind], 1)
        _started[kind] = None


def _begin(kind: str, steps: list[tuple[str, str]] | None = None) -> None:
    """重置某类任务的状态并开始计时。"""
    with _lock:
        st = _state[kind]
        st.clear()
        st.update({
            "running": True, "phase": "starting", "message": "",
            "steps": [], "logs": [],
            "started_at": _now(), "finished_at": None, "elapsed": 0.0,
            "ok": None, "error": None, "hint": None, "result": None,
        })
        if kind == "push":
            st["article_id"] = None
        for key, label in steps or []:
            st["steps"].append(
                {"key": key, "label": label, "status": "pending", "detail": ""}
            )
        _started[kind] = time.time()


def _copy_state(kind: str) -> dict:
    return {k: ([dict(x) for x in v] if isinstance(v, list) else v)
            for k, v in _state[kind].items()}


def snapshot() -> dict:
    """当前登录/推送状态(供前端轮询)。"""
    with _lock:
        out = {"login": _copy_state("login"), "push": _copy_state("push")}
        for kind in ("login", "push"):
            if _state[kind]["running"] and _started.get(kind):
                out[kind]["elapsed"] = round(time.time() - _started[kind], 1)
    out["session"] = session_status()
    return out


# ---------------- 工作进程调度 ----------------

def _tail_text(path: Path, lines: int = 25) -> str:
    """取文件末尾若干行。用于把工作进程的输出摘要带回状态里。

    没有这个之前,子进程静默死亡时只会看到「工作进程未返回结果」——
    完全无法定位。现在失败一定会附带子进程自己的输出。
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception:  # noqa: BLE001
        return ""


def _apply_event(kind: str, ev: dict, box: dict) -> None:
    """把工作进程的一条事件落到状态上。"""
    et = ev.get("type")
    if et == "step":
        _step(kind, ev.get("key", ""), ev.get("label", ""),
              ev.get("status", ""), ev.get("detail", ""))
        if kind == "login" and ev.get("status") == "running":
            _mark("login", "scanning",
                  ev.get("detail") or "浏览器已打开,请用手机微信扫码")
    elif et == "log":
        msg = str(ev.get("message", ""))
        _mark(kind, _state[kind].get("phase") or "", msg)
        logger.info("[%s worker] %s", kind, msg)
    elif et == "result":
        box["result"] = ev


def _spawn_runner(kind: str, args: list[str], timeout: float,
                  attempts: int = 3) -> dict | None:
    """执行工作进程;进程**自身没能启动**时自动重试。

    为什么需要重试:该环境下子进程启动是**间歇性**失败的 —— 同一条命令,
    有时 1 秒内就正常产出事件,有时 0.4 秒就退出且零输出(退出码 1)。
    业务层面的失败(扫码超时、选择器改版等)带明确错误信息,不重试;
    只有连结果事件都拿不到时才重试。
    """
    last: dict | None = None
    for i in range(1, attempts + 1):
        last = _run_runner_once(kind, args, timeout)
        if not (last or {}).get("_no_result"):
            return last
        logger.warning("工作进程第 %d/%d 次未能正常启动,准备重试", i, attempts)
        if i < attempts:
            time.sleep(1.2)
    return last


def _run_runner_once(kind: str, args: list[str], timeout: float) -> dict | None:
    """在独立进程里执行 Playwright 任务,事件**从文件读**。

    为什么不让服务进程直接跑 Playwright:见 `app/pusher/runner.py` 顶部说明 ——
    同一环境里独立脚本能启动、服务进程里驱动会秒退。

    为什么不用 stdout 管道:在部分宿主环境里,服务进程 spawn 出的子进程
    管道读取会失效 —— 子进程实际跑完了(退出码正常),父进程却一行都读不到,
    对外只表现为「工作进程未返回结果」。走文件不依赖管道,事后还能回放。
    """
    import os

    logs_dir = settings.data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "pusher_worker.log"
    ev_path = logs_dir / "runner_events.jsonl"
    argv = [sys.executable, "-m", "app.pusher.runner", *args]
    logger.info("启动推送工作进程: %s (%s)", " ".join(args), sys.executable)

    try:
        ev_path.write_text("", encoding="utf-8")   # 清掉上一次的事件
    except Exception:  # noqa: BLE001
        pass
    env = dict(os.environ)
    env["PUSHER_EVENT_FILE"] = str(ev_path)

    box: dict = {"result": None}
    state = {"killed": False}
    seen = 0

    def _drain(cur: int) -> int:
        """读事件文件的新增行并落到状态上,返回已读行数。"""
        try:
            lines = ev_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:  # noqa: BLE001
            return cur
        for raw in lines[cur:]:
            line = raw.strip()
            if not line.startswith("{"):
                continue
            try:
                _apply_event(kind, json.loads(line), box)
            except ValueError:
                continue
        return len(lines)

    t0 = time.time()
    with open(log_path, "a", encoding="utf-8", errors="replace") as errf:
        errf.write(f"\n===== {_now()} | {' '.join(args)} =====\n")
        # Windows 上优先让工作进程**脱离父进程的 job / 进程组**。
        # 宿主若对进程组设了限制,子进程会秒退且一行输出都不留 ——
        # 这正是「工作进程未返回结果」最常见的真实成因。
        # 逐级降档尝试,任何一档成功即用。
        base = dict(cwd=str(PROJECT_ROOT), env=env,
                    stdout=subprocess.DEVNULL, stderr=errf)
        flag_sets: list[int] = [0]
        if sys.platform == "win32":
            newgrp = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            flag_sets = [newgrp | 0x01000000, newgrp, 0]  # 叠加 BREAKAWAY_FROM_JOB
        proc = None
        spawn_err = ""
        for flags in flag_sets:
            try:
                proc = subprocess.Popen(argv, creationflags=flags, **base)
                logger.info("工作进程已启动(flags=0x%X, pid=%s)", flags, proc.pid)
                break
            except Exception as e:  # noqa: BLE001
                spawn_err = f"{type(e).__name__}: {e}"
                logger.warning("以 flags=0x%X 启动失败: %s", flags, spawn_err)
        if proc is None:
            return {
                "_no_result": True,          # 进程自身没起来 → 值得重试
                "ok": False,
                "error": f"工作进程无法启动:{spawn_err}",
                "hint": "检查 .venv 是否完整、项目目录是否可写,然后重启后端。",
                "worker_tail": _tail_text(log_path, 12),
            }
        state = {"killed": False}

        def _watchdog():
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                state["killed"] = True
                logger.error("推送工作进程超时(%ss),强制结束", timeout)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()
        while True:
            seen = _drain(seen)
            if proc.poll() is not None:
                break
            time.sleep(0.4)
        seen = _drain(seen)          # 收尾:把进程最后写的事件也读进来
        rc = proc.returncode
        elapsed = round(time.time() - t0, 2)
        wd.join(timeout=5)

    if box["result"] is not None:
        return box["result"]

    if state["killed"]:
        return {
            "ok": False,
            "error": f"工作进程超时({int(timeout)} 秒)被强制结束",
            "hint": "浏览器可能卡住:稍后重试;反复出现请查看 data/logs/pusher_worker.log。",
            "worker_tail": _tail_text(log_path, 20),
        }

    tail = _tail_text(log_path, 25)
    try:
        events = "\n".join(
            ev_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
        )
    except Exception:  # noqa: BLE001
        events = ""
    why = f"工作进程没有送回结果(退出码 {rc},耗时 {elapsed}s)。"
    if not events.strip() and not tail.strip():
        if elapsed < 3:
            why += "它几乎立刻退出且没有任何输出 —— 子进程被宿主进程组拦下了。"
        else:
            why += "它没有产生任何输出。"
    return {
        "_no_result": True,              # 进程自身没起来 → 值得重试
        "ok": False,
        "error": why,
        "hint": "详情见 data/logs/pusher_worker.log 与 data/logs/runner_events.jsonl;"
                "若两者都为空,换一个普通终端重启后端再试。",
        "worker_tail": (tail + ("\n--- 事件 ---\n" + events if events.strip() else "")).strip(),
    }


# ---------------- 会话信息 ----------------

def _pushed_today(db_path: str | None = None) -> int:
    conn = connect(db_path) if db_path else connect()
    try:
        r = conn.execute(
            """SELECT COUNT(*) FROM run_logs
               WHERE stage='review' AND status='push_draft'
                 AND date(started_at)=date('now','localtime')"""
        ).fetchone()
        return r[0] if r else 0
    finally:
        conn.close()


def session_status(db_path: str | None = None) -> dict:
    """登录会话状态。

    注意:`session_exists` 只表示「本地有会话文件」,真实有效性在推送时才会暴露
    (文件存在但已被服务端失效是常见情况),所以 UI 要同时显示「上次验证时间」。
    """
    p = state_path()
    exists = session_exists()
    saved_at = None
    if exists:
        saved_at = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    if db_path:
        # 配置与「上次验证时间」都从同一个库读 —— 否则传了 db_path 却仍读线上配置,
        # 会出现"日限额莫名变成线上的值"这类假失败。
        conn = connect(db_path)
        try:
            cfg = get_all_config(conn)
            row = conn.execute(
                "SELECT value FROM configs WHERE key=?", (SESSION_VERIFIED_KEY,)
            ).fetchone()
            verified = json.loads(row["value"]) if row else None
        finally:
            conn.close()
    else:
        cfg = get_all_config()
        verified = get_config(SESSION_VERIFIED_KEY)
    max_age = int(cfg.get("mp_session_max_age_days", 5) or 5)
    expired = False
    if verified:
        try:
            expired = datetime.now() - datetime.strptime(str(verified), "%Y-%m-%d %H:%M:%S") > timedelta(days=max_age)
        except ValueError:
            expired = False
    limit = int(cfg.get("mp_daily_draft_limit", 5) or 5)
    used = _pushed_today(db_path)
    return {
        "session_exists": exists,
        "session_file": str(p),
        "session_saved_at": saved_at,
        "last_verified_at": verified,
        "expired": expired,
        "max_age_days": max_age,
        "author": cfg.get("mp_author", ""),
        "cover_image": cfg.get("mp_cover_image", ""),
        "cover_ready": bool(cfg.get("mp_cover_image"))
        and Path(str(cfg.get("mp_cover_image"))).exists(),
        # 推送时真正会用哪种封面:自动生成优先于静态默认图(与 runner 的判断保持一致)
        "cover_auto": bool(cfg.get("mp_cover_enabled")),
        "cover_style": cfg.get("mp_cover_style", ""),
        "cover_tag": cfg.get("mp_cover_tag", ""),
        "daily_limit": limit,
        "pushed_today": used,
        "remaining_today": max(0, limit - used),
    }


# ---------------- 扫码登录 ----------------

_last_login_ts: float = 0.0


def start_login(scan_wait: int | None = None, force: bool = False) -> dict:
    """启动扫码登录(后台线程 + 可见浏览器),立即返回。

    force=True 用于「重新扫码登录」:忽略已保存的会话,强制展示二维码。
    """
    global _last_login_ts
    cfg = get_all_config()
    wait = int(scan_wait or cfg.get("mp_scan_wait_seconds", 180) or 180)
    now = time.time()
    with _lock:
        if _state["login"]["running"]:
            return {"ok": False, "error": "登录流程已在进行中",
                    "hint": "请在弹出的浏览器窗口用手机微信扫码。"}
        if _state["push"]["running"]:
            return {"ok": False, "error": "正在推送草稿,浏览器被占用",
                    "hint": "等推送结束后再登录,避免两个浏览器实例互相干扰。"}
        # 防抖:失败会瞬间把 running 置回 False,连点会堆出一串工作进程互相抢资源
        if now - _last_login_ts < 3.0:
            return {"ok": False, "error": "操作过于频繁",
                    "hint": "上一次登录刚发起,请等 3 秒后再试。"}
        _last_login_ts = now
    _begin("login")
    _mark("login", "opening",
          f"正在打开二维码窗口(扫码等待 {wait} 秒)…" if force
          else f"正在启动浏览器(扫码等待 {wait} 秒)…")
    threading.Thread(target=_login_worker, args=(wait, force), daemon=True).start()
    tip = "已打开二维码窗口,请用手机微信扫码" if force else "已启动浏览器,请用手机微信扫码"
    return {"ok": True, "message": f"{tip}(限 {wait} 秒)"}


def _login_worker(wait: int, force: bool = False) -> None:
    try:
        _mark("login", "scanning",
              "正在打开二维码窗口,请留意弹出的浏览器…" if force
              else "正在启动浏览器,请留意弹出的二维码窗口…")
        args = ["login", "--scan-wait", str(wait)]
        if force:
            args.append("--force")
        res = _spawn_runner("login", args, timeout=wait + 240)
        if res and res.get("ok"):
            set_config(SESSION_VERIFIED_KEY, _now(), updated_by="pusher")
            _mark("login", "done", "登录成功,会话已保存")
            _finish("login", True)
            logger.info("扫码登录成功,会话已保存")
            return
        err = (res or {}).get("error") or "工作进程未返回结果"
        _mark("login", "failed", err)
        # 把工作进程自己的输出带进状态,否则失败时无从下手
        tail = (res or {}).get("worker_tail") or ""
        for line in tail.splitlines()[-12:]:
            if line.strip():
                _mark("login", "worker", line[:200])
        _finish("login", False, error=err, hint=(res or {}).get("hint"))
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        logger.exception("扫码登录失败")
        _mark("login", "failed", msg)
        _finish("login", False, error=msg,
                hint="浏览器未能正常打开:确认本机能访问 mp.weixin.qq.com;"
                     "若弹出窗口被拦截,请检查安全软件。详细堆栈见 data/logs/pusher_worker.log。")


def logout() -> dict:
    """清除本地会话(下次推送需重新扫码)。"""
    p = state_path()
    if p.exists():
        p.unlink()
    set_config(SESSION_VERIFIED_KEY, "", updated_by="pusher")
    return {"ok": True, "message": "已清除本地登录会话,下次推送需要重新扫码"}


# ---------------- 推送草稿 ----------------

def start_push(article_id: int, headless: bool = False) -> dict:
    """后台把指定文章推送到公众号草稿箱,立即返回。"""
    cfg = get_all_config()
    limit = int(cfg.get("mp_daily_draft_limit", 5) or 5)
    used = _pushed_today()
    if used >= limit:
        return {"ok": False, "error": f"今日草稿推送已达上限({used}/{limit})",
                "hint": "风控保护:请明天再推,或到「控制面板」调高 mp_daily_draft_limit。"}
    with _job_lock:
        with _lock:
            if _state["push"]["running"]:
                return {"ok": False, "error": "已有推送任务在执行",
                        "hint": "Playwright 浏览器不能并发,请等当前任务结束。"}
            if _state["login"]["running"]:
                return {"ok": False, "error": "正在扫码登录,浏览器被占用",
                        "hint": "等登录流程结束后再推送。"}
        _begin("push", [
            ("load", "读取稿件"),
            ("session", "校验登录会话"),
            ("login", "打开公众号后台"),
            ("fill", "填写标题/正文"),
            ("cover", "上传封面"),
            ("save", "保存草稿"),
            ("record", "回执落库"),
        ])
        with _lock:
            _state["push"]["article_id"] = article_id
    threading.Thread(target=_push_worker, args=(article_id, headless), daemon=True).start()
    return {"ok": True, "article_id": article_id, "message": "推送任务已启动,可在进度面板查看"}


def _push_worker(article_id: int, headless: bool) -> None:
    global _last_push_ts
    cfg = get_all_config()
    try:
        # ① 读取稿件(父进程先做前置校验,避免白开浏览器)
        _mark("push", "load", "读取稿件…")
        from app.review.service import get_article

        art = get_article(article_id)
        if not art:
            _step("push", "load", "读取稿件", "failed", "文章不存在")
            _finish("push", False, error=f"文章不存在: #{article_id}", hint="刷新文章库后重试。")
            return
        _step("push", "load", "读取稿件", "success", f"{(art.get('title') or '')[:30]}")

        # ② 会话校验(本地无会话就不用启动浏览器了)
        _mark("push", "session", "校验登录会话…")
        if not session_exists():
            _step("push", "session", "校验登录会话", "failed", "本地无会话")
            _finish("push", False, error="尚未登录公众号(本地没有登录会话)",
                    hint="先点「扫码登录」,用手机微信扫码一次;之后会话可复用数天。")
            return
        _step("push", "session", "校验登录会话", "success", "存在本地会话")

        # ③ 风控:与上次推送保持随机间隔
        interval = random.uniform(
            float(cfg.get("mp_interval_min", 2) or 2),
            float(cfg.get("mp_interval_max", 5) or 5),
        )
        since = time.time() - _last_push_ts if _last_push_ts else 1e9
        if since < interval:
            wait = round(interval - since, 1)
            _mark("push", "wait", f"风控保护:等待 {wait} 秒后继续…")
            time.sleep(wait)

        # ④ 交给独立进程执行(Playwright 隔离,详见 runner.py)
        _mark("push", "login", "正在打开公众号后台…")
        args = ["push", "--article-id", str(article_id)]
        if headless:
            args.append("--headless")
        res = _spawn_runner("push", args, timeout=900)

        if res and res.get("ok"):
            r = res.get("result") or {}
            set_config(SESSION_VERIFIED_KEY, _now(), updated_by="pusher")
            # ⑤ 回执落库(状态变更唯一出口)
            _mark("push", "record", "写入回执…")
            from app.review.service import push_to_drafts

            rec = push_to_drafts(article_id, r.get("draft_url", ""), reviewer="auto_pusher")
            _step("push", "record", "回执落库", "success", "状态 → 已推送")
            _last_push_ts = time.time()
            _mark("push", "done", "推送完成")
            _finish("push", True, result={
                "article_id": article_id,
                "draft_url": rec.get("draft_url", ""),
                "cover_uploaded": bool(r.get("cover_uploaded")),
                "warnings": r.get("warnings") or [],
            })
            logger.info("文章 #%s 已推送草稿: %s", article_id, rec.get("draft_url"))
            return

        err = (res or {}).get("error") or "工作进程未返回结果"
        logger.error("推送失败: %s", err)
        _mark("push", "failed", err)
        tail = (res or {}).get("worker_tail") or ""
        for line in tail.splitlines()[-12:]:
            if line.strip():
                _mark("push", "worker", line[:200])
        _finish("push", False, error=err, hint=(res or {}).get("hint"))
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        logger.exception("推送异常: %s", msg)
        _mark("push", "failed", msg)
        _finish("push", False, error=msg,
                hint="属未预期异常,请查看 data/logs/screenshots/ 的截图与 data/logs/pusher_worker.log。")


def _try_spawn() -> dict:
    """能否从本进程派生子进程,并读到它的输出。

    登录/推送都靠独立工作进程跑 Playwright。若宿主进程被限制派生子进程,
    子进程会**静默死亡**(既无 stdout 也无 stderr),对外只表现为
    「工作进程未返回结果」—— 完全无从下手。这个自检把那种情况一次暴露出来。
    """
    import subprocess
    import sys as _sys

    try:
        p = subprocess.run(
            [_sys.executable, "-c",
             "import sys; print('SPAWN_OK'); sys.stderr.write('stderr-ok\\n')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, cwd=str(PROJECT_ROOT),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": p.returncode == 0 and "SPAWN_OK" in (p.stdout or ""),
        "returncode": p.returncode,
        "stdout": (p.stdout or "").strip()[:120],
        "stderr": (p.stderr or "").strip()[:120],
    }


def _try_playwright() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        b = pw.chromium.launch(headless=True)
        ver = b.version
        b.close()
        pw.stop()
        return True, ver
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def diagnose(in_thread: bool = False) -> dict:
    """推送环境自检:Playwright 能否在本进程启动 + 关键环境变量。

    用途:推送失败时快速区分「代码问题」与「进程环境问题」。
    Playwright 的驱动是个 Node 子进程;通过 in_thread 还能区分
    「框架线程池里能跑、我们自建的线程里跑不了」这类线程环境差异。
    """
    import os
    import sys
    import threading

    info: dict = {
        "python": sys.executable,
        "thread": threading.current_thread().name,
        "in_thread": in_thread,
        "node_options": os.environ.get("NODE_OPTIONS"),
        "electron_run_as_node": os.environ.get("ELECTRON_RUN_AS_NODE"),
        "playwright_nodejs_path": os.environ.get("PLAYWRIGHT_NODEJS_PATH"),
        "session_exists": session_exists(),
    }
    if in_thread:
        box: dict = {}

        def run():
            ok, detail = _try_playwright()
            box["ok"], box["detail"] = ok, detail

        t = threading.Thread(target=run, name="diagnose-thread")
        t.start()
        t.join()
        info["playwright_ok"], info["playwright_detail"] = box.get("ok"), box.get("detail")
    else:
        ok, detail = _try_playwright()
        info["playwright_ok"], info["playwright_detail"] = ok, detail
        info["chromium_version"] = detail if ok else None
    info["spawn"] = _try_spawn()
    return info


# ---------------- 封面生成 ----------------

def cover_path(article_id: int, style: str = "") -> str | None:
    """已生成的封面文件路径(供前端预览);没有则返回 None。"""
    from app.pusher import cover as cover_mod

    d = settings.data_dir / "covers"
    for st in ([style] if style else list(cover_mod.COVER_STYLES)):
        p = d / f"{article_id}_{st}.png"
        if p.exists():
            return str(p)
    return None


def generate_cover(article_id: int, style: str = "") -> dict:
    """为文章生成封面图(Playwright 必须在独立进程里渲染)。

    **同步等待** —— 一张封面通常 2~5 秒,不值得再为它做一套轮询状态机。
    前端点「生成封面」时短暂等待即可。
    """
    with _lock:                       # 只做检查
        if _state["cover"]["running"]:
            return {"ok": False, "error": "封面生成已在进行中", "hint": "稍等几秒后再试。"}
    # ⚠️ `_begin` / `_mark` / `_step` 内部也会获取 `_lock`,而它是普通 Lock(不可重入)。
    # 必须在 `with _lock` **之外**调用,否则自己把自己锁死(表现为请求永远挂住)。
    _begin("cover", [("cover", "生成封面")])
    with _lock:
        _state["cover"]["article_id"] = article_id

    args = ["cover", "--article-id", str(article_id)]
    if style:
        args += ["--style", style]
    try:
        res = _spawn_runner("cover", args, timeout=180) or {}
        if res.get("ok"):
            result = res.get("result") or {}
            _finish("cover", True, result=result)
            return {"ok": True, "article_id": article_id, **result}
        err = res.get("error") or "封面生成失败"
        _finish("cover", False, error=err, hint=res.get("hint"))
        return {"ok": False, "error": err, "hint": res.get("hint"),
                "worker_tail": res.get("worker_tail")}
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        logger.exception("封面生成异常")
        _finish("cover", False, error=msg, hint="查看后端日志与 data/logs/runner_events.jsonl。")
        return {"ok": False, "error": msg}


def plan_b_text(article_id: int, db_path: str | None = None) -> dict:
    """方案 B:导出可人工粘贴的 Markdown 文本 + 带内联样式的 HTML。"""
    from app.pusher.client import plan_b_export, plan_b_html
    from app.review.service import get_article

    art = get_article(article_id, db_path)
    if not art:
        return {"ok": False, "error": f"文章不存在: #{article_id}"}
    return {
        "ok": True,
        "article_id": article_id,
        "title": art.get("title", ""),
        "markdown": plan_b_export(art),
        "html": plan_b_html(art),
    }
