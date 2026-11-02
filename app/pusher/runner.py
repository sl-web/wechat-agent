"""Playwright 推送工作进程:在**独立进程**里执行登录 / 推送草稿。

为什么必须单独开进程(踩过的坑):
- Playwright 的 Node 驱动对宿主进程状态敏感。同一个环境里,独立脚本能正常启动,
  在 uvicorn 服务进程里却会出现驱动秒退(Connection closed while reading the driver),
  外层伪装成 `'PlaywrightContextManager' object has no attribute '_playwright'`;
- 浏览器卡死/崩溃不会拖垮 API 服务;
- 进程退出即彻底释放浏览器与临时 profile。

用法:
    python -m app.pusher.runner login --scan-wait 600
    python -m app.pusher.runner push  --article-id 24 [--headless]

输出协议:stdout 每行一个 JSON 事件,父进程逐行解析。
    {"type": "step",   "key": "login", "label": "打开公众号后台", "status": "running", "detail": ""}
    {"type": "log",    "message": "..."}
    {"type": "result", "ok": true,  "result": {...}}
    {"type": "result", "ok": false, "error": "...", "hint": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

# 事件文件:除 stdout 之外,把事件**同时**追加写到文件。
#
# 为什么需要它:在服务进程里 spawn 出来的子进程,stdout 管道读取在某些
# 宿主环境下会失效 —— 子进程明明跑完了(退出码正常),父进程却一行都读不到,
# 对外只表现为「工作进程未返回结果」。改走文件后不依赖管道,过程也可回放。
_EVENT_FILE = os.environ.get("PUSHER_EVENT_FILE")


def _append_event(line: str) -> None:
    if not _EVENT_FILE:
        return
    try:
        # 每次 append 单独开关文件:避免长期持有句柄导致内容残留在缓冲区
        with open(_EVENT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def emit(**kw) -> None:
    line = json.dumps(kw, ensure_ascii=False)
    print(line, flush=True)
    _append_event(line)


def _setup_logging() -> None:
    import logging

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _hint_for(msg: str) -> str:
    if "超时" in msg:
        return "扫码超时:重新发起登录,并在浏览器窗口出现后用手机微信尽快扫码。"
    if "选择器" in msg or "改版" in msg:
        return "页面可能已改版:按 app/pusher/selectors.yaml 的注释更新选择器,或改用方案 B 手动粘贴。"
    if "driver" in msg or "playwright" in msg.lower():
        return "浏览器驱动启动失败:在项目根目录执行 `playwright install chromium` 后重试。"
    return "可改用方案 B 导出排好版的正文手动粘贴;失败截图见 data/logs/screenshots/。"


def _load_article(article_id: int) -> dict | None:
    from app.review.service import get_article

    return get_article(article_id)


def cmd_login(args) -> int:
    from app.pusher.client import WechatPusher

    force = bool(getattr(args, "force", False))
    emit(type="step", key="login", label="打开公众号后台", status="running",
         detail="正在启动浏览器…" if not force else "正在打开二维码窗口…")

    def _on_qrcode():
        emit(type="step", key="login", label="打开公众号后台", status="running",
             detail="二维码已显示,请用手机微信扫码")

    pusher = WechatPusher(headless=False)
    try:
        pusher.open(scan_wait_seconds=args.scan_wait, force=force, on_qrcode=_on_qrcode)
        emit(type="step", key="login", label="打开公众号后台", status="success",
             detail="已登录,会话已保存")
        emit(type="result", ok=True, result={})
        return 0
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        emit(type="step", key="login", label="打开公众号后台", status="failed", detail=msg[:180])
        emit(type="result", ok=False, error=msg, hint=_hint_for(msg))
        return 1
    finally:
        pusher.close()


def cmd_push(args) -> int:
    from app.core.config import get_all_config
    from app.pusher.client import WechatPusher
    from app.pusher.markdown import article_to_markdown

    cfg = get_all_config()
    art = _load_article(args.article_id)
    if not art:
        emit(type="result", ok=False, error=f"文章不存在: #{args.article_id}",
             hint="刷新文章库后重试。")
        return 1

    md = article_to_markdown(art)
    if len(md.strip()) < 50:
        emit(type="result", ok=False, error="稿件正文过短,拒绝推送",
             hint="回到文章处理窗口检查正文,或重新生成。")
        return 1
    emit(type="step", key="load", label="读取稿件", status="success",
         detail=f"{(art.get('title') or '')[:30]}({len(md)} 字)")

    author = str(cfg.get("mp_author", "") or "").strip()
    cover = str(cfg.get("mp_cover_image", "") or "").strip() or None
    scan_wait = int(cfg.get("mp_scan_wait_seconds", 600) or 600)

    # 自动生成封面(优先于 mp_cover_image):模板渲染,零版权风险。
    # 生成失败只记警告 —— 封面是"尽力而为",不该阻断推送(草稿仍可保存,后台再补)。
    cover_warn = ""
    if cfg.get("mp_cover_enabled"):
        emit(type="step", key="cover", label="生成封面", status="running",
             detail="按标题渲染封面模板…")
        try:
            from app.pusher import cover as cover_mod

            gen = cover_mod.generate(
                art,
                style=str(cfg.get("mp_cover_style") or cover_mod.DEFAULT_STYLE),
                brand=author,
                tag=str(cfg.get("mp_cover_tag") or ""),
            )
            if gen.get("wide"):
                cover = gen["wide"]
                emit(type="step", key="cover", label="生成封面", status="success",
                     detail=f"已生成({gen.get('style')})")
            else:
                cover_warn = gen.get("wide_error") or "封面生成未返回文件"
                emit(type="step", key="cover", label="生成封面", status="failed",
                     detail=cover_warn[:150])
        except Exception as e:  # noqa: BLE001
            cover_warn = f"{type(e).__name__}: {e}"
            emit(type="step", key="cover", label="生成封面", status="failed",
                 detail=cover_warn[:150])

    pusher = WechatPusher(headless=args.headless)
    try:
        emit(type="step", key="login", label="打开公众号后台", status="running",
             detail="复用登录会话…")
        pusher.open(scan_wait_seconds=scan_wait)
        emit(type="step", key="login", label="打开公众号后台", status="success", detail="已登录")

        emit(type="step", key="fill", label="填写标题/正文", status="running",
             detail="已进入图文编辑器")
        if cover:
            src = "自动生成" if cfg.get("mp_cover_enabled") else "默认封面"
            emit(type="step", key="cover", label="上传封面", status="running",
                 detail=f"上传{src}…")
        elif not cover_warn:
            emit(type="step", key="cover", label="上传封面", status="skipped",
                 detail="未配置封面,人工在后台补")

        res = pusher.push_draft(art.get("title", ""), author, md, cover)
        if cover_warn:
            res.setdefault("warnings", []).append(f"封面自动生成失败:{cover_warn}")

        emit(type="step", key="fill", label="填写标题/正文", status="success",
             detail=f"{res.get('body_chars', 0)} 字")
        if res.get("cover_uploaded"):
            emit(type="step", key="cover", label="上传封面", status="success", detail="默认封面已设置")
        elif cover:
            warn = next((w for w in (res.get("warnings") or []) if "封面" in w), "封面未设置成功")
            emit(type="step", key="cover", label="上传封面", status="failed", detail=warn)
        emit(type="step", key="save", label="保存草稿", status="success",
             detail=res.get("draft_url") or "已保存")
        emit(type="result", ok=True, result=res)
        return 0
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        emit(type="step", key="save", label="保存草稿", status="failed", detail=msg[:180])
        emit(type="result", ok=False, error=msg, hint=_hint_for(msg))
        return 1
    finally:
        pusher.close()


def cmd_cover(args) -> int:
    """只生成封面、不推送 —— 供前端预览与本地验证。"""
    from app.core.config import get_all_config
    from app.pusher import cover as cover_mod

    cfg = get_all_config()
    art = _load_article(args.article_id)
    if not art:
        emit(type="result", ok=False, error=f"文章不存在: #{args.article_id}",
             hint="刷新文章库后重试。")
        return 1

    emit(type="step", key="cover", label="生成封面", status="running",
         detail="按标题渲染封面模板…")
    gen = cover_mod.generate(
        art,
        style=args.style or str(cfg.get("mp_cover_style") or cover_mod.DEFAULT_STYLE),
        brand=str(cfg.get("mp_author") or ""),
        tag=str(cfg.get("mp_cover_tag") or ""),
    )
    if not gen.get("wide"):
        err = gen.get("wide_error") or "封面生成未返回文件"
        emit(type="step", key="cover", label="生成封面", status="failed", detail=err[:180])
        emit(type="result", ok=False, error=err,
             hint="确认已执行 `playwright install chromium`;也可改用 mp_cover_image 指定静态封面。")
        return 1
    emit(type="step", key="cover", label="生成封面", status="success", detail=str(gen["wide"]))
    emit(type="result", ok=True, result=gen)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.pusher.runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="扫码登录并保存会话")
    p_login.add_argument("--scan-wait", type=int, default=600)
    p_login.add_argument("--force", action="store_true",
                         help="忽略本地会话,强制重新扫码")
    p_login.set_defaults(func=cmd_login)

    p_push = sub.add_parser("push", help="把文章推送到草稿箱")
    p_push.add_argument("--article-id", type=int, required=True)
    p_push.add_argument("--headless", action="store_true")
    p_push.set_defaults(func=cmd_push)

    p_cover = sub.add_parser("cover", help="只生成封面图(不推送)")
    p_cover.add_argument("--article-id", type=int, required=True)
    p_cover.add_argument("--style", default="", help="dark / light / blocks,留空读配置")
    p_cover.set_defaults(func=cmd_cover)

    args = parser.parse_args(argv)
    _setup_logging()
    # 先留一条"我起来了"的痕迹:若之后在 import / 启动浏览器阶段崩掉,
    # 父进程至少知道子进程确实启动过,而不是完全没跑。
    _append_event(json.dumps(
        {"type": "log", "message": f"工作进程已启动: {' '.join(argv or sys.argv[1:])}"},
        ensure_ascii=False,
    ))
    try:
        return args.func(args)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        emit(type="result", ok=False, error="工作进程异常退出",
             hint="请查看后端日志中的完整堆栈。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
