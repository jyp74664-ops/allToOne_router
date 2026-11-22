"""FlowGate - 高性能 AI 模型网关"""
import logging
import time
from pathlib import Path
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# 必须在任何 httpx.Client 创建之前执行：
# conda 环境装的是 brotlicffi 1.2.0.0，它解码上游 br 压缩响应时会抛
#   "brotli: decoder process called with data when 'can_accept_more_data()' is False"
# 导致拉模型列表（Kilo Code 这类 600KB+ 的大响应必触发）和转发请求全部失败，
# 表现为「请求超时 / 上游拉取失败」。这里统一不向上游声明 br/zstd，
# httpx 的 ACCEPT_ENCODING 是模块级常量、Client 初始化时读取，改它全局生效。
# ---------------------------------------------------------------------------
import httpx._client as _httpx_client

_httpx_client.ACCEPT_ENCODING = "gzip, deflate"

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from config.settings import settings
from database.engine import init_db_lifespan
from api import admin_router, proxy_router, responses_router, webhook_router
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
            async with httpx.AsyncClient(timeout=30) as client:
                await _sync_rl(client, ModelMetaService())
        except Exception as e:
            logger.warning("initial rate-limit sync failed: %s", e)

    # 保存任务强引用，避免被 GC 导致后台任务静默停止
    app.state.bg_tasks = []
    app.state.bg_tasks.append(asyncio.create_task(_init_tasks()))
    app.state.bg_tasks.append(asyncio.create_task(_auto_validate_loop()))
    # 启动时清理过期记录
    try:
        from services.usage_service import cleanup_old_records
        await cleanup_old_records()
    except Exception as e:
        logger.warning("startup cleanup failed: %s", e)
    logger.info("FlowGate started")
    yield
    for task in getattr(app.state, "bg_tasks", []):
        task.cancel()
    logger.info("FlowGate shutdown")


async def _auto_validate_loop():
    """后台自动验证循环：根据配置定时执行全量健康检测。"""
    import asyncio
    from services.health_service import run_full_check, update_provider_status, health_state
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
                # 健康历史已由 run_full_check 内部统一写入，这里不再重复写
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

# 请求日志中间件：合并 uvicorn access log 与业务成功日志
access_logger = logging.getLogger("flowgate.access")

async def log_request_middleware(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    client_info = f"{request.client.host}:{request.client.port}" if request.client else "unknown"
    query = request.url.query
    full_path = f"{request.url.path}?{query}" if query else request.url.path
    access_logger.info(
        '%s - "%s %s HTTP/1.1" %d %.3fs',
        client_info, request.method, full_path, response.status_code, process_time
    )
    return response


# 请求日志中间件（不启用 CORS：管理页与 API 同源，杜绝跨站凭据窃取）
app.middleware("http")(log_request_middleware)

# 注册路由
app.include_router(admin_router, prefix="")
app.include_router(proxy_router, prefix="")
app.include_router(responses_router, prefix="")
app.include_router(webhook_router, prefix="")
app.include_router(metrics_router, prefix="")

# 静态资源（拆分后的 CSS/JS）
class NoCacheStaticFiles(StaticFiles):
    """静态资源一律要求浏览器协商缓存。

    StaticFiles 默认不发 Cache-Control，浏览器会按启发式规则自行缓存，
    导致改了 CSS/JS 后必须 Ctrl+F5 才生效（已多次踩坑）。
    加 no-cache 后每次请求带 If-Modified-Since，未变返回 304（几十字节），
    变了立刻拿新的。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", NoCacheStaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "ok", "service": "flowgate"}


def render_dashboard() -> str:
    """渲染管理面板（不注入任何密钥，登录后经 /api/client-key 获取）。"""
    template = Path(__file__).parent / "templates" / "index.html"
    html = template.read_text(encoding="utf-8")
    html = html.replace("{{ local_api_key }}", "")
    html = html.replace("{{app_version}}", app.version)
    return html


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    """返回管理面板"""
    return HTMLResponse(render_dashboard(), headers={"Cache-Control": "no-store"})


@app.get("/templates/index.html", response_class=HTMLResponse)
async def dashboard_template() -> str:
    """兼容旧地址并返回已渲染的管理面板"""
    return HTMLResponse(render_dashboard(), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8777,
        reload=False,
        log_level="info",
        access_log=False,
    )
