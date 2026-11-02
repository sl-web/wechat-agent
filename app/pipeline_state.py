"""流水线运行时状态:供前端轮询展示「执行进度 / 当前阶段 / 耗时 / 实时日志 / 失败原因」。

背景:流水线是同步长任务(抓取数十秒、真实 LLM 生成 1~2 分钟),
前端只能等 HTTP 返回,过程中看不到任何进展,失败时也拿不到原因。
本模块在进程内维护一份当前运行的快照,配合 GET /api/pipeline/status 轮询。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

STAGE_LABEL = {
    "fetch": "抓取热点",
    "analyze": "分析归纳",
    "generate": "生成文章",
    "push": "推送草稿",
    "backfill": "效果回流",
    "clean": "知识库清理",
    "all": "一键流水线",
}

# 一键流水线的固定阶段链(与 pipeline.run_pipeline 的 all 分支一致)
CHAIN = ["fetch", "analyze", "generate"]

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "stage": None,
    "label": None,
    "run_id": None,
    "started_at": None,
    "finished_at": None,
    "elapsed": 0.0,
    "steps": [],
    "logs": [],
    "ok": None,
    "error": None,
    "hint": None,
    "result": None,
}
_started_ts: float | None = None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def begin(stage: str, run_id: str, steps: list[str] | None = None) -> None:
    """开始一次运行。steps 为要展示的阶段链(默认按 stage 推导)。"""
    global _started_ts
    chain = steps or (CHAIN if stage == "all" else [stage])
    with _lock:
        _state.update(
            running=True,
            stage=stage,
            label=STAGE_LABEL.get(stage, stage),
            run_id=run_id,
            started_at=_now(),
            finished_at=None,
            elapsed=0.0,
            ok=None,
            error=None,
            hint=None,
            result=None,
            logs=[],
            steps=[
                {"stage": s, "label": STAGE_LABEL.get(s, s), "status": "pending", "detail": ""}
                for s in chain
            ],
        )
        _started_ts = time.time()


def mark(step: str, status: str, detail: str = "") -> None:
    """更新某阶段状态。status: pending/running/success/failed/skipped"""
    with _lock:
        for s in _state["steps"]:
            if s["stage"] == step:
                s["status"] = status
                s["detail"] = detail
        _state["logs"].append(
            {"t": _now(), "stage": step, "label": STAGE_LABEL.get(step, step),
             "status": status, "detail": detail}
        )
        _state["logs"] = _state["logs"][-80:]


def finish(ok: bool, error: str | None = None, hint: str | None = None,
           result: dict | None = None) -> None:
    """结束运行,记录最终结果与失败原因。"""
    global _started_ts
    with _lock:
        _state.update(
            running=False,
            finished_at=_now(),
            elapsed=round(time.time() - _started_ts, 1) if _started_ts else 0.0,
            ok=ok,
            error=error,
            hint=hint,
            result=result,
        )
        _started_ts = None


def snapshot() -> dict:
    """当前状态快照(供轮询)。"""
    with _lock:
        s = {k: v for k, v in _state.items()}
        s["steps"] = [dict(x) for x in _state["steps"]]
        s["logs"] = [dict(x) for x in _state["logs"]]
        if _state["running"] and _started_ts:
            s["elapsed"] = round(time.time() - _started_ts, 1)
    return s
