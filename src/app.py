"""Invoicing Assistant — 企业微信群聊开票助手

解耦架构:
- wecom_gateway/ : 企业微信 API 接入层
- agent/         : Agent 智能决策层
- tools/         : 业务工具层 (OCR/Excel/税编)
- db/            : 数据库层
- api/           : API 路由层
- frontend/      : 前端页面
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import get_settings
from src.api.routes import router as api_router
from src.db.models import get_engine

# ── 日志 ─────────────────────────────────────────────────────

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("invoicing")


# ── 生命周期 ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭"""
    settings = get_settings()
    logger.info("=" * 60)
    logger.info("Invoicing Assistant 启动中...")
    logger.info("WeCom Ready: %s", settings.wecom_ready)
    logger.info("Callback Ready: %s", settings.callback_ready)
    logger.info("AI Ready: %s (%s)", settings.ai_ready, settings.ai_model)
    logger.info("OCR Engine: %s", settings.ocr_engine)
    logger.info("DB: %s", settings.effective_db_path)
    logger.info("Frontend URL: %s", settings.effective_frontend_url)
    logger.info("=" * 60)

    # 初始化数据库
    engine = await get_engine()
    logger.info("数据库初始化完成")

    yield

    # 关闭
    if engine:
        await engine.dispose()
    logger.info("Invoicing Assistant 已关闭")


# ── 应用实例 ──────────────────────────────────────────────────

app = FastAPI(
    title="Invoicing Assistant",
    description="企业微信群聊开票助手 — 解耦架构",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件
frontend_dir = Path(__file__).resolve().parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir / "assets")), name="static")

# 路由
app.include_router(api_router)


# ── 入口 ─────────────────────────────────────────────────────

def main():
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "src.app:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
