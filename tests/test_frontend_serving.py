"""单进程托管的回归测试:后端直接把前端构建产物挂在同一端口。

背景(为什么要做):以前跑起来要开两个窗口 —— 后端 8000 + 前端 Vite 5173。
桌面启动器要的是"双击一个图标就能用",所以改成后端顺带托管 `dist`,
一个地址 http://localhost:8000 同时提供界面与接口。

⚠️ 这几条测试**不依赖仓库里到底有没有 dist**:把 dist 目录换成临时目录再断言,
否则"没跑过 npm run build 的环境"会让测试变红(而代码其实没问题)。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import mount_frontend


def _app_with_index(dist: Path) -> FastAPI:
    a = FastAPI()

    @a.get("/health")
    def health() -> dict:
        return {"ok": True}

    @a.get("/api/ping")
    def ping() -> dict:
        return {"pong": True}

    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><div id=app>界面</div>", encoding="utf-8")
    (dist / "assets").mkdir(exist_ok=True)
    (dist / "assets" / "a.js").write_text("console.log(1)", encoding="utf-8")
    assert mount_frontend(a, dist) is True
    return a


def test_mount_frontend_returns_false_without_dist(tmp_path):
    """没构建过就别挂 —— 挂了会把所有请求变成 404。"""
    from app.main import mount_frontend as mf

    assert mf(FastAPI(), tmp_path / "not-built") is False


def test_root_serves_index_html(tmp_path):
    with TestClient(_app_with_index(tmp_path / "dist")) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "id=app" in r.text


def test_static_assets_are_served(tmp_path):
    with TestClient(_app_with_index(tmp_path / "dist")) as c:
        r = c.get("/assets/a.js")
        assert r.status_code == 200
        assert "console.log" in r.text


def test_api_routes_win_over_static_mount(tmp_path):
    """⚠️ 关键:挂在 "/" 上不能把 /api 抢走(靠"先注册的具体路由优先")。"""
    with TestClient(_app_with_index(tmp_path / "dist")) as c:
        assert c.get("/api/ping").json() == {"pong": True}
        assert c.get("/health").json() == {"ok": True}
        # 未定义的接口仍要 404,不能被静态托管兜成 200 的 index.html
        assert c.get("/api/nope").status_code == 404
