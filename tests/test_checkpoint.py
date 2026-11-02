"""断点记录器测试。"""
from __future__ import annotations

from pathlib import Path

from app.core.checkpoint import RunRecorder, new_run_id
from app.core.db import init_db


def test_run_recorder_logs(tmp_path: Path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    run_id = new_run_id()
    with RunRecorder(run_id, str(db_path)) as rec:
        rec.log("fetch", "checkpoint", "抓取完成 20 条")
        rec.log("fetch", "success", "OK")
        assert rec.last_checkpoint("fetch") is True
        rec.log("analyze", "failed", "聚类失败")
        assert rec.last_checkpoint("analyze") is False


def test_run_id_unique():
    ids = {new_run_id() for _ in range(100)}
    assert len(ids) == 100


def test_success_does_not_overwrite_diagnostic_checkpoints(tmp_path: Path):
    """success 不能吞掉诊断用的 checkpoint。

    真实踩过:一次生成中途记了「第 1 次校验未通过:命中违禁词 ['最优']」,
    最终成功时这条被 success **覆盖**掉 —— 于是事后既统计不出重试率,
    也查不出"为什么这次慢了一倍"。运行日志必须是只追加的。
    """
    from app.core.db import connect

    db_path = tmp_path / "test.db"
    init_db(db_path)
    run_id = new_run_id()
    with RunRecorder(run_id, str(db_path)) as rec:
        rec.log("generate", "checkpoint", "开始生成")
        rec.log("generate", "checkpoint", "第 1 次校验未通过:命中违禁词: ['最优']")
        rec.log("generate", "success", "文章 #42 生成成功")

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status, detail FROM run_logs WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    finally:
        conn.close()
    details = [r["detail"] for r in rows]
    assert len(rows) == 3, details
    assert any("第 1 次校验未通过" in (d or "") for d in details), "重试证据被覆盖了"
    assert any("生成成功" in (d or "") for d in details)


def test_last_checkpoint_still_works_after_append_only(tmp_path: Path):
    """改成只追加后,"该环节最后状态"的判定语义不变。"""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    run_id = new_run_id()
    with RunRecorder(run_id, str(db_path)) as rec:
        rec.log("analyze", "checkpoint", "开始分析")
        assert rec.last_checkpoint("analyze") is False      # 只是"进行中"
        rec.log("analyze", "success", "12 个主题")
        assert rec.last_checkpoint("analyze") is True
        rec.log("analyze", "failed", "重新跑一次失败")
        assert rec.last_checkpoint("analyze") is False
