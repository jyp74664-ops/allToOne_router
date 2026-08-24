"""FlowGate - 高性能 AI 模型网关"""
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from config.settings import settings
from database.engine import init_db_lifespan
from api import admin_router, proxy_router, webhook_router
from api.metrics import router as metrics_router

# 日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("flowgate")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    await init_db_lifespan()
    # 启动时异步同步速率限制
    import asyncio
    from services.rate_limit_service import sync_rate_limits as _sync_rl
    from services.meta_service import ModelMetaService
    import httpx

    async def _init_tasks():
        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                await _sync_rl(client, ModelMetaService())
        except Exception as e:
            logger.warning("initial rate-limit sync failed: %s", e)
        asyncio.create_task(_auto_validate_loop())

    asyncio.create_task(_init_tasks())
    logger.info("FlowGate started")
    yield
    logger.info("FlowGate shutdown")


async def _auto_validate_loop():
    """后台自动验证循环：根据配置定时执行全量健康检测。"""
    import asyncio
    from services.health_service import run_full_check, update_provider_status, health_state
    from services.usage_service import append_health_history
    logger.info("auto_validate_loop started")
    while True:
        cfg = settings.load()
        interval = cfg.auto_validate_interval or 1800
        await asyncio.sleep(interval)
        if cfg.auto_validate:
            try:
                logger.info("auto_validate: running full check")
                results = await run_full_check()
                health_state.health_status.update(results)
                for name in {key.split("||", 1)[0] for key in results}:
                    update_provider_status(name)
                await append_health_history(results)
                import time
                settings._last_check_time = time.time()
                ok = sum(1 for v in results.values() if v.get("status") == "ok")
                fail = len(results) - ok
                logger.info("auto_validate done: %d ok / %d fail", ok, fail)
            except Exception:
                logger.exception("auto_validate failed")


app = FastAPI(
    title="FlowGate",
    description="高性能 AI 模型网关 - 聚合多源免费额度，智能轮询自动切换",
    version="0.1.0",
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

# 注册路由
app.include_router(admin_router, prefix="")
app.include_router(proxy_router, prefix="")
app.include_router(webhook_router, prefix="")
app.include_router(metrics_router, prefix="")

# 静态文件（前端构建产物）
frontend_dist = Path(__file__).parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "service": "flowgate"}


def render_dashboard() -> str:
    """渲染带本地 API 密钥的管理面板"""
    template = Path(__file__).parent / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    html = html.replace("{{ local_api_key }}", settings.load().local_api_key)
    html = html.replace("{{app_version}}", app.version)
    return html


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    """返回管理面板"""
    return render_dashboard()


@app.get("/templates/index.html", response_class=HTMLResponse)
async def dashboard_template() -> str:
    """兼容旧地址并返回已渲染的管理面板"""
    return render_dashboard()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8777,
        reload=False,
        log_level="info",
    )
