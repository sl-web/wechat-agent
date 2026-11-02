"""调度中心:APScheduler 定时任务(人工设计 cron)+ 手动触发(PRD 4.3)。"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

# 环节执行函数注册表(由各里程碑模块注册)
# key: "fetch" / "analyze" / "generate" / "push" / "backfill"
STAGE_HANDLERS: dict[str, callable] = {}


def register_stage(stage: str, handler: callable) -> None:
    """注册一个可调度的环节处理器。"""
    STAGE_HANDLERS[stage] = handler


def run_stage(stage: str) -> dict:
    """手动触发单个环节。"""
    if stage not in STAGE_HANDLERS:
        return {"ok": False, "error": f"未注册环节: {stage}"}
    handler = STAGE_HANDLERS[stage]
    return handler()


def _apply_schedule(name: str, cron: str, actions: str) -> None:
    """把一个 cron 表达式注册为 APScheduler 任务。

    cron 支持: "08:00" 或标准 5 段 cron "0 8 * * *"
    """
    job_id = f"job_{name}"
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    parts = cron.split()
    if len(parts) == 2 and ":" in parts[0]:  # 简单 "HH:MM" 形式
        hour, minute = parts[0].split(":")
        day = parts[1] if parts[1] != "*" else "*"
        scheduler.add_job(
            run_stage,
            trigger="cron",
            hour=int(hour),
            minute=int(minute),
            args=[actions],
            id=job_id,
            replace_existing=True,
            name=name,
        )
    else:  # 标准 5 段 cron
        scheduler.add_job(
            run_stage,
            trigger="cron",
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            args=[actions],
            id=job_id,
            replace_existing=True,
            name=name,
        )


def load_schedules_from_db() -> None:
    """从 schedules 表加载启用的任务。"""
    from app.core.db import connect

    conn = connect()
    try:
        rows = conn.execute(
            "SELECT name, cron, actions FROM schedules WHERE enabled=1"
        ).fetchall()
        for r in rows:
            try:
                _apply_schedule(r["name"], r["cron"], r["actions"])
            except Exception:  # noqa: BLE001
                continue
    finally:
        conn.close()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
    load_schedules_from_db()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
