"""FastAPI 入口:启动建表 + 注册路由 + 调度器 + 托管前端构建产物。

运行:uvicorn app.main:app --port 8000
- 开发:前端另开 `npm run dev`(5173),由 Vite 把 /api 代理到这里;
- 日常使用:前端 `npm run build` 后,本进程**直接把 dist 托管在同一端口** ——
  一个窗口、一个地址(http://localhost:8000),桌面启动器就是靠这个。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_all_config
from app.core.db import init_db
from app.core.logger import get_logger
from app.scheduler import start_scheduler, stop_scheduler

logger = get_logger("main")

# 前端构建产物(与后端同级目录)
_FRONTEND_DIST = Path(__file__).resolve().parents[1] / "wechat-agent-frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("初始化数据库...")
    init_db()
    cfg = get_all_config()
    logger.info("配置已加载: %d 项", len(cfg))
    start_scheduler()
    logger.info("调度器已启动")
    yield
    stop_scheduler()
    logger.info("调度器已停止")


app = FastAPI(title="公众号文章自动化 Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "wechat-agent-backend", "version": "0.1.0"}


# 各业务路由:控制面板/审核台/看板/流水线(统一挂在 /api 下)
# 前端项目位于同级目录 wechat-agent-frontend,dev 端口 5173(Vite),通过 proxy /api 转发到此处 8000
from app.web import router as web_router  # noqa: E402

app.include_router(web_router.router, prefix="/api")


def mount_frontend(app: FastAPI, dist_dir: Path) -> bool:
    """把前端构建产物挂到根路径。返回是否挂上。

    ⚠️ 挂载在 "/" 上会兜住所有未匹配路径,所以必须**最后**调用(在 /api、/health、/docs 之后):
    Starlette 按注册顺序匹配,先注册的具体路由优先,不会把 /api 抢走。
    前端用 hash 路由(#/articles)、接口用相对路径 /api,同源托管完全成立。

    抽成独立函数是为了**可测**:dist 存不存在取决于"有没有跑过 npm run build",
    直接对生产 app 断言 routes 会让测试依赖环境 —— 本项目刚在别处踩过这个坑。
    """
    if not (dist_dir / "index.html").is_file():
        return False
    app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="frontend")
    return True


# ---- 托管前端构建产物(必须**最后**挂载)----
if mount_frontend(app, _FRONTEND_DIST):
    logger.info("已托管前端构建产物: %s", _FRONTEND_DIST)
else:
    logger.warning("未找到前端构建产物(%s),界面请用 npm run dev 启动", _FRONTEND_DIST)
