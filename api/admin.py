"""管理接口路由"""
import asyncio
import threading
import time
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import select
import httpx

from config.settings import settings
from api.auth import (
    verify_admin, verify_admin_password, issue_session_token, security,
    is_local_request, credential_is_valid,
)
from services.health_service import check_model, fetch_models, health_state, describe_fetch_error
from services.provider_service import ProviderService
from services.router_manager import RouterService
from services.usage_service import (
    append_usage,
    read_usage,
    append_health_history,
    cleanup_old_records,
)
from services.meta_service import ModelMetaService
from services.rate_limit_service import sync_rate_limits

logger = logging.getLogger("flowgate.api")

router = APIRouter(prefix="/api")


# ============================================================
# 检测互斥
# ============================================================
# 全量检测和单服务商检测共用一个「检测位」：连点两次会起两轮并发检测，
# 双倍消耗上游额度，而且两边都在写 health_records / check_logs，结果互相覆盖。
# 用时间戳标志而不是 asyncio.Lock：Lock 会绑定事件循环，测试里多个 TestClient
# 各自的事件循环复用同一个 Lock 会抛「bound to a different event loop」。
_CHECK_GUARD = threading.Lock()
_CHECK_STATE = {"running": False, "kind": "", "started": 0.0, "token": ""}
CHECK_STALE_SECONDS = 3 * 3600  # 超过 3 小时视为上一轮异常退出、标记失效


def _busy_message() -> str:
    kind = _CHECK_STATE.get("kind") or "检测"
    started = _CHECK_STATE.get("started") or 0
    if started:
        mins = max(0, int((time.time() - started) / 60))
        return f"已有一轮检测在进行中（{kind}，已运行 {mins} 分钟），请等它结束后再点"
    return f"已有一轮检测在进行中（{kind}），请等它结束后再点"


def _acquire_check(kind: str) -> str:
    """占用检测位，返回 token；已被占用返回空串。"""
    now = time.time()
    with _CHECK_GUARD:
        if _CHECK_STATE["running"]:
            if now - _CHECK_STATE["started"] < CHECK_STALE_SECONDS:
                return ""
            logger.warning(
                "检测互斥标记已过期（已运行 %.0f 分钟），强制释放",
                (now - _CHECK_STATE["started"]) / 60,
            )
        token = f"{kind}-{uuid.uuid4().hex[:6]}"
        _CHECK_STATE.update(running=True, kind=kind, started=now, token=token)
        return token


def _release_check(token: str) -> None:
    """释放检测位（只释放自己持有的那个 token）。"""
    with _CHECK_GUARD:
        if _CHECK_STATE["running"] and _CHECK_STATE["token"] == token:
            _CHECK_STATE.update(running=False, kind="", started=0.0, token="")


class AdminLoginIn(BaseModel):
    password: str


# 登录防爆破：60 秒内失败 5 次锁定 1 分钟（按来源 IP 计）
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW = 60
_LOGIN_GUARD = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}   # ip -> 失败时间戳列表
_LOGIN_LOCKED: dict[str, float] = {}        # ip -> 解锁时间戳


@router.get("/admin/auth-status")
async def admin_auth_status(request: Request):
    """公开的探测端点：告诉前端本机免登录、局域网是否已登录。"""
    if is_local_request(request):
        return {"need_login": False, "local": True}
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if credential_is_valid(token):
        return {"need_login": False, "local": False}
    return {"need_login": True, "local": False}


@router.post("/admin/login")
async def admin_login(body: AdminLoginIn, request: Request):
    """管理端登录：校验独立管理密码，签发短期会话 token。

    本机访问无需登录；此端点主要供局域网使用。

    防爆破：同一 IP 60 秒内失败 5 次即锁定 1 分钟，且每次失败有递增延迟
    （0.4s / 0.8s / 1.2s …）。管理密码是 32 位随机串，正常不可能连错，
    出现连续失败基本就是脚本在试。
    """
    from api.auth import get_client_ip

    ip = get_client_ip(request) or "unknown"
    now = time.time()
    with _LOGIN_GUARD:
        locked_until = _LOGIN_LOCKED.get(ip, 0)
        if locked_until > now:
            wait = int(locked_until - now) + 1
            raise HTTPException(
                429,
                f"登录失败次数过多，请 {wait} 秒后再试",
                headers={"Retry-After": str(wait)},
            )
        fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < LOGIN_WINDOW]
        _LOGIN_FAILS[ip] = fails

    if not verify_admin_password(body.password):
        with _LOGIN_GUARD:
            fails = _LOGIN_FAILS.setdefault(ip, [])
            fails.append(time.time())
            count = len(fails)
            if count >= LOGIN_MAX_FAILS:
                _LOGIN_LOCKED[ip] = time.time() + LOGIN_WINDOW
                logger.warning("管理登录连续失败 %d 次，锁定 IP %s %d 秒", count, ip, LOGIN_WINDOW)
        # 递增延迟：把爆破成本从「每秒几百次」压到「每分钟个位数」
        await asyncio.sleep(min(0.4 * count, 2.0))
        raise HTTPException(status_code=401, detail="管理密码错误")

    with _LOGIN_GUARD:
        _LOGIN_FAILS.pop(ip, None)
        _LOGIN_LOCKED.pop(ip, None)
    token = issue_session_token()
    return {"token": token, "token_type": "bearer"}


@router.get("/client-key")
async def get_client_key(_=Depends(verify_admin)):
    """登录后返回客户端 API key（供管理页展示，不写入 HTML）。"""
    return {"api_key": settings.load().local_api_key}


# ============================================================
# Pydantic 模型
# ============================================================
class ProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str
    models: list[str] = []
    free_only: bool = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    models: Optional[list[str]] = None
    disabled_models: Optional[list[str]] = None
    free_only: Optional[bool] = None
    check_enabled: Optional[bool] = None


class ToggleModelIn(BaseModel):
    model: str
    enabled: bool


class AutoValidateIn(BaseModel):
    enabled: bool


class ContextLimitUpdate(BaseModel):
    model: str
    context_length: int


# ============================================================
# 健康状态接口
# ============================================================
@router.get("/health-status")
async def get_health_status(_=Depends(verify_admin)):
    """返回当前健康状态摘要"""
    return {
        "status": dict(health_state.health_status),
        "provider_status": dict(health_state.provider_api_status),
        "circuit_breaker": dict(health_state.circuit_breaker),
        "last_check_time": getattr(settings, "_last_check_time", 0),
    }


class ClearFailIn(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    all: bool = False


@router.post("/health-status/clear-fail-counts")
async def clear_fail_counts(
    body: Optional[ClearFailIn] = None,
    provider: Optional[str] = None,
    _=Depends(verify_admin),
):
    """清除失败标记。

    精确模式：body 传 provider + model，只清一个模型。
    兼容模式：仅传 provider，清该 provider 下全部；显式 all=true 才清全部。
    """
    body = body or ClearFailIn()
    target_provider = body.provider or provider
    target_model = body.model
    if body.all:
        return {"cleared": health_state.clear_all_fail_counts()}

    if target_provider and target_model:
        key = f"{target_provider}||{target_model}"
        if key in health_state.circuit_breaker:
            health_state.clear_fail_count(key)
            return {"cleared": 1}
        return {"cleared": 0}

    if target_provider:
        cleared = 0
        for k in [k for k in health_state.circuit_breaker if k.startswith(target_provider + "||")]:
            health_state.clear_fail_count(k)
            cleared += 1
        return {"cleared": cleared}

    # 没有任何限定条件：拒绝，避免误清空全部
    raise HTTPException(status_code=400, detail="请指定 provider/model，或显式传 all=true")


@router.get("/poll-status")
async def poll_status(_=Depends(verify_admin)):
    """轮询状态"""
    from database.models import Provider
    from database.engine import db
    
    async with db.SessionLocal() as session:
        providers = await session.execute(select(Provider))
        total_models = sum(len((p.models or [])) for p in providers.scalars().all())
    return {
        "last_check_time": getattr(settings, "_last_check_time", 0),
        "total_models": total_models,
    }


# ============================================================
# 提供商管理接口
# ============================================================
@router.get("/providers")
async def list_providers(_=Depends(verify_admin)):
    """列出所有提供商"""
    svc = ProviderService()
    return await svc.list_all()


@router.post("/providers")
async def add_provider(data: ProviderIn, _=Depends(verify_admin)):
    """添加提供商"""
    svc = ProviderService()
    try:
        result = await svc.create(data.model_dump())
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/providers/{name}")
async def update_provider(name: str, data: ProviderUpdate, _=Depends(verify_admin)):
    """更新提供商"""
    svc = ProviderService()
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        return {"ok": True}
    result = await svc.update(name, update_data)
    if not result:
        raise HTTPException(404, "未找到")
    return result


@router.delete("/providers/{name}")
async def delete_provider(name: str, _=Depends(verify_admin)):
    """删除提供商"""
    svc = ProviderService()
    ok = await svc.delete(name)
    if not ok:
        raise HTTPException(404, "未找到")
    return {"ok": True}


@router.post("/providers/{name}/toggle-model")
async def toggle_model(name: str, data: ToggleModelIn, _=Depends(verify_admin)):
    """启用/禁用模型"""
    svc = ProviderService()
    try:
        return await svc.toggle_model(name, data.model, data.enabled)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/providers/{name}/available-models")
async def get_available_models(name: str, _=Depends(verify_admin)):
    """获取上游可用模型列表（不保存模型列表）。

    顺带刷新该提供商的「免费模型集合」：检测阶段要靠它跳过收费模型，
    而调用方（模型管理弹窗）本来就要拉一次上游 /models，这里直接复用，
    不额外发请求。
    """
    from database.models import Provider
    from database.engine import db
    import httpx

    async with db.SessionLocal() as session:
        result = await session.execute(select(Provider).where(Provider.name == name))
        p = result.scalar_one_or_none()
    if not p:
        raise HTTPException(404, "未找到")

    meta = ModelMetaService()
    try:
        free_out: set[str] = set()
        diag: dict = {}
        async with httpx.AsyncClient(timeout=15, verify=settings.load().verify_ssl) as client:
            models = await fetch_models(
                client, p.base_url, p.api_key, free_only=False,
                aliases=meta.aliases, context_limits=meta.context_limits,
                free_out=free_out, diag_out=diag,
            )
        if models is None:
            # 失败原因分层：DNS / 连接 / TLS / 代理 / HTTP 状态码各有各的处理办法，
            # 全部压成「网络/超时/Key 无效」等于什么都没说（实测 OpenAI 直连与
            # 走本地代理都不可达时，用户无法判断该改哪里）。
            reason = describe_fetch_error(diag.get("error") or None, diag.get("status"))
            return {
                "ok": False,
                "models": [],
                "detail": f"上游模型列表拉取失败：{reason}。本地已勾选的模型不受影响。",
                "diagnosis": {
                    "stage": reason,
                    "http_status": diag.get("status"),
                    "error": diag.get("error") or "",
                    "base_url": p.base_url,
                },
            }
        if not models:
            return {"ok": False, "models": [], "detail": "上游没有返回任何可用模型。"}
        # 上游判定结果有变化才写库，避免每次开弹窗都写一次
        if sorted(free_out) != sorted(p.free_models or []):
            async with db.SessionLocal() as session:
                row = (await session.execute(
                    select(Provider).where(Provider.name == name)
                )).scalar_one_or_none()
                if row:
                    row.free_models = sorted(free_out)
                    await session.commit()
        return {"ok": True, "models": models}
    except Exception as e:
        logger.warning("available-models fetch failed for %s: %s", name, e)
        return {
            "ok": False,
            "models": [],
            "detail": f"上游模型列表拉取失败：{describe_fetch_error(e)}。本地已勾选的模型不受影响。",
            "diagnosis": {"stage": describe_fetch_error(e), "error": f"{type(e).__name__}: {e}"},
        }


@router.post("/providers/{name}/fetch-models")
async def refresh_models(name: str, _=Depends(verify_admin)):
    """刷新模型列表（同时刷新免费模型集合，供检测阶段过滤收费模型）"""
    svc = ProviderService()
    try:
        async with httpx.AsyncClient(timeout=30, verify=settings.load().verify_ssl) as client:
            models = await svc.refresh_models(
                name, client, ModelMetaService().aliases, ModelMetaService().context_limits
            )
        return {"ok": True, "models": models}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/providers/sync-free-models")
async def sync_all_free_models(_=Depends(verify_admin)):
    """批量同步免费模型"""
    svc = ProviderService()
    async with httpx.AsyncClient(timeout=30, verify=settings.load().verify_ssl) as client:
        results = await svc.sync_free_models(
            client, ModelMetaService().aliases, ModelMetaService().context_limits,
            health_state.health_status,
        )
    return {"ok": True, "results": results}


# ============================================================
# 路由组管理接口
# ============================================================
@router.get("/routers")
async def get_routers(_=Depends(verify_admin)):
    """获取所有路由组"""
    svc = RouterService()
    return JSONResponse(
        {"ok": True, "data": await svc.list_all()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/routers")
async def save_routers(request: Request, _=Depends(verify_admin)):
    """保存路由组"""
    body = await request.json()
    svc = RouterService()
    try:
        await svc.save_all(body)
    except ValueError as exc:
        # 别名重复等问题：明确 400 告知，不要静默写进一条会让请求 500 的数据
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.get("/routers/aliases")
async def get_router_aliases(_=Depends(verify_admin)):
    """返回路由组别名映射 {alias: chinese_name}"""
    svc = RouterService()
    return JSONResponse({"ok": True, "data": await svc.get_aliases()})


@router.delete("/routers/{name}")
async def delete_router(name: str, _=Depends(verify_admin)):
    """删除路由组"""
    svc = RouterService()
    ok = await svc.delete(name)
    if not ok:
        raise HTTPException(404, "未找到")
    return {"ok": True}


# ============================================================
# 健康探测接口
# ============================================================
@router.post("/check/all")
async def check_all(_=Depends(verify_admin)):
    """全量探测（流式返回进度）"""
    from services.health_service import run_full_check
    from fastapi.responses import StreamingResponse
    import json
    
    progress_log = []
    
    def on_progress(provider, model, status, current, total):
        progress_log.append({
            "provider": provider,
            "model": model,
            "status": status,
            "current": current,
            "total": total
        })
    
    # 启动后台检测任务（先占检测位，避免连点两次并发跑两轮）
    import asyncio
    token = _acquire_check("全量检测")
    if not token:
        raise HTTPException(409, _busy_message())

    async def _run_full():
        try:
            return await run_full_check(on_progress=on_progress)
        finally:
            _release_check(token)

    try:
        results_task = asyncio.create_task(_run_full())
    except Exception:
        _release_check(token)
        raise
    
    async def generate_progress():
        """流式输出进度"""
        last_count = 0
        while True:
            if len(progress_log) > last_count:
                for i in range(last_count, len(progress_log)):
                    yield f"data: {json.dumps(progress_log[i], ensure_ascii=False)}\n\n"
                last_count = len(progress_log)
            
            if results_task.done():
                # 输出最终结果
                results = results_task.result()
                yield f"data: {json.dumps({'done': True, 'results': results}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.1)
    
    return StreamingResponse(generate_progress(), media_type="text/event-stream")


@router.post("/check/provider/{name}")
async def check_provider(name: str, _=Depends(verify_admin)):
    """仅检查指定提供商的所有模型（流式返回进度）"""
    from services.health_service import health_state
    from database.models import Provider
    from database.engine import db
    from fastapi.responses import StreamingResponse
    import json
    import asyncio
    
    # 确保数据库已初始化
    db.get_engine()
    
    # 验证提供商存在
    async with db.SessionLocal() as session:
        result = await session.execute(select(Provider).where(Provider.name == name))
        provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(404, "未找到该提供商")
    if getattr(provider, "check_enabled", True) is False:
        raise HTTPException(400, "该提供商已关闭检测，请先打开开关")
    
    # 没有可检测的模型时别静默返回「0 正常 / 0 异常」，否则用户会以为检测坏了
    from services.health_service import checkable_models
    if not checkable_models(provider):
        if not (provider.models or []):
            raise HTTPException(
                400,
                f"{name} 当前没有任何模型（模型列表为空），请先打开「⚙️ 模型」勾选并保存，再点检测",
            )
        raise HTTPException(
            400,
            f"{name} 勾选了「仅拉取免费模型」，但已配置的模型在上游都判为收费模型，已全部跳过；"
            "如需检测收费模型，请取消该勾选后保存",
        )
    
    # 只检查当前提供商的模型
    async def check_single_provider():
        """仅检查指定提供商"""
        from sqlalchemy import select
        from services.meta_service import ModelMetaService
        from services.health_service import UPSTREAM_TIMEOUT
        from config.settings import settings
        import httpx
        
        meta = ModelMetaService()
        results = {}
        retire_list: list[str] = []   # EOL(410) 确认下线，立即移除
        pending_404: list[str] = []   # 404 待复测确认
        # 与全量检测口径一致：只探测已勾选的模型；勾了「仅拉取免费模型」的
        # 提供商还要再滤掉收费模型，避免探测收费接口产生费用。
        models_to_check = checkable_models(provider)
        total = len(models_to_check)
        current = 0
        
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, verify=settings.load().verify_ssl) as client:
            for model in models_to_check:
                key = f"{provider.name}||{model}"
                
                # 检查是否在冷却期内
                if health_state.is_model_rate_limited(key):
                    remaining = health_state.get_model_cooldown_remaining(key)
                    if on_progress:
                        on_progress(provider.name, model, "cooldown", current + 1, total)
                    current += 1
                    continue
                
                # 上一轮是 deleted 也照常探测：退役要由「本轮 404 + 60 秒后复测仍 404」
                # 共同确认，避免一次抖动就把模型永久删掉（与全量检测口径一致）
                prev_status = health_state.health_status.get(key, {}).get("status")
                
                result = await check_model(
                    client, provider.base_url, provider.api_key, model, meta.aliases
                )
                results[key] = result
                health_state.health_status[key] = result
                health_state.update_quality(key, result)
                
                status = result.get("status", "unknown")
                
                # EOL(410) 立即退役；404 只记候选，等 60 秒复测确认
                if status == "eol":
                    retire_list.append(model)
                elif status == "deleted":
                    pending_404.append(model)
                
                if status == "rate_limited":
                    retry_after = result.get("retry_after")
                    health_state.record_model_rate_limit(key, retry_after)
                
                if status == "deleted":
                    health_state.failed_models[key] = time.time()
                
                # 与全量检测(run_full_check)保持一致的计数策略：
                # - deleted(404)/rate_limited(429)：不是稳定性失败，不触发熔断
                # - 只有真正的 fail/error(5xx/超时/连接错误) 才计失败
                if status == "ok":
                    if prev_status == "deleted":
                        health_state.failed_models.pop(key, None)
                    health_state.record_success(key)
                elif status in ("deleted", "rate_limited"):
                    logger.info("  [跳过计数] %s | %s -> %s", provider.name, model, status)
                else:
                    health_state.record_fail(key)
                
                current += 1
                if on_progress:
                    on_progress(provider.name, model, status, current, total)

            # 404 候选复测：等 60 秒后重测，仍 404 的才并入退役名单（与全量检测一致）
            if pending_404:
                from services.health_service import reconfirm_retire_candidates
                confirmed = await reconfirm_retire_candidates(
                    client, meta.aliases,
                    {provider.name: pending_404}, results,
                    {provider.name: provider}, on_progress=on_progress,
                )
                retire_list.extend(confirmed.get(provider.name, []))

        # 统一移除已确认下线（410）与复测仍 404 的模型
        from services.health_service import retire_models
        if retire_list:
            await retire_models(name, retire_list)
        # 单服务商日志写入固定槽位（覆盖上一家）
        from services.usage_service import save_check_log, append_health_history
        await save_check_log("provider", results, scope=name)
        # 健康历史落库：实时监控列表的成功率/延迟/速度都靠它统计
        await append_health_history(results)
        return results
    
    # 进度回调
    progress_log = []
    def on_progress(provider_name, model, status, current, total):
        if provider_name == name:
            progress_log.append({
                "provider": provider_name,
                "model": model,
                "status": status,
                "current": current,
                "total": total
            })
    
    # 运行检查（与全量检测共用检测位，同一时间只允许一轮）
    token = _acquire_check(f"单服务商 {name}")
    if not token:
        raise HTTPException(409, _busy_message())

    async def _run_single():
        try:
            return await check_single_provider()
        finally:
            _release_check(token)

    try:
        results_task = asyncio.create_task(_run_single())
    except Exception:
        _release_check(token)
        raise
    
    async def generate_progress():
        """流式输出进度"""
        last_count = 0
        while True:
            if len(progress_log) > last_count:
                for i in range(last_count, len(progress_log)):
                    yield f"data: {json.dumps(progress_log[i], ensure_ascii=False)}\n\n"
                last_count = len(progress_log)
            
            if results_task.done():
                results = results_task.result()
                yield f"data: {json.dumps({'done': True, 'results': results}, ensure_ascii=False)}\n\n"
                break
            await asyncio.sleep(0.1)
    
    return StreamingResponse(generate_progress(), media_type="text/event-stream")

@router.post("/check/{name}/{model}")
async def manual_check(name: str, model: str, _=Depends(verify_admin)):
    """手动探测单个模型"""
    from database.models import Provider
    from database.engine import db
    
    async with db.SessionLocal() as session:
        result = await session.execute(
            select(Provider).where(Provider.name == name)
        )
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(404, "未找到")
    
    # 勾了「仅拉取免费模型」的提供商，单模型检查也不探测收费模型（会产生费用）
    from services.health_service import is_paid_model
    if is_paid_model(p, model):
        raise HTTPException(
            400, "该提供商勾选了「仅拉取免费模型」，此模型为收费模型，已跳过检测"
        )
    meta = ModelMetaService()
    async with httpx.AsyncClient(timeout=30, verify=settings.load().verify_ssl) as client:
        check_result = await check_model(
            client, p.base_url, p.api_key, model, meta.aliases
        )
    key = f"{name}||{model}"
    health_state.health_status[key] = check_result
    # 单模型探测同样计入健康历史，实时监控里能立刻看到这次结果
    from services.usage_service import append_health_history
    await append_health_history({key: check_result})
    return check_result



# ============================================================
# 自动验证接口
# ============================================================
@router.get("/auto-validate")
async def get_auto_validate(_=Depends(verify_admin)):
    """获取自动验证开关状态"""
    cfg = settings.load()
    return {
        "enabled": bool(cfg.auto_validate),
        "interval": cfg.auto_validate_interval,
    }


@router.post("/auto-validate")
async def set_auto_validate(body: AutoValidateIn, _=Depends(verify_admin)):
    """设置自动验证开关"""
    cfg = settings.load()
    cfg.auto_validate = bool(body.enabled)
    settings.save(cfg)
    logger.info("auto_validate set to %s", cfg.auto_validate)
    return {
        "enabled": cfg.auto_validate,
        "interval": cfg.auto_validate_interval,
    }


@router.get("/merge-reasoning")
async def get_merge_reasoning(_=Depends(verify_admin)):
    """获取推理内容合并开关状态"""
    return {"enabled": bool(settings.load().merge_reasoning)}


@router.post("/merge-reasoning")
async def set_merge_reasoning(body: AutoValidateIn, _=Depends(verify_admin)):
    """设置推理内容合并开关"""
    cfg = settings.load()
    cfg.merge_reasoning = bool(body.enabled)
    settings.save(cfg)
    logger.info("merge_reasoning set to %s", cfg.merge_reasoning)
    return {"enabled": cfg.merge_reasoning}


class LangHintIn(BaseModel):
    mode: str  # auto / off


@router.get("/lang-hint")
async def get_lang_hint(_=Depends(verify_admin)):
    """获取回复语言提示注入策略（auto / off）"""
    mode = str(getattr(settings.load(), "lang_hint", "auto") or "auto").lower()
    return {"mode": mode if mode in ("auto", "off") else "auto"}


@router.post("/lang-hint")
async def set_lang_hint(body: LangHintIn, _=Depends(verify_admin)):
    """设置回复语言提示注入策略。

    auto：按用户消息语种注入「请用中文/英文回答」；off：完全不注入。
    JSON 输出、代码生成、工具调用这类任务不需要语言提示，反而是干扰。
    """
    mode = str(body.mode or "").lower()
    if mode not in ("auto", "off"):
        raise HTTPException(400, "mode 只能是 auto 或 off")
    cfg = settings.load()
    cfg.lang_hint = mode
    settings.save(cfg)
    logger.info("lang_hint set to %s", mode)
    return {"mode": mode}


@router.get("/check-concurrency")
async def get_check_concurrency(_=Depends(verify_admin)):
    """获取全量检测并发度"""
    cfg = settings.load()
    return {"concurrency": int(getattr(cfg, "check_concurrency", 5) or 5)}


@router.post("/check-concurrency")
async def set_check_concurrency(body: dict, _=Depends(verify_admin)):
    """设置全量检测并发度（1~32）。

    并发越高检测越快，但免费额度提供商对 RPM 敏感：16 家 × 每家几十个模型
    同时打过去很容易集体 429，默认 5 是折中值。
    """
    try:
        value = int((body or {}).get("concurrency"))
    except (TypeError, ValueError):
        raise HTTPException(400, "concurrency 必须是整数")
    if value < 1 or value > 32:
        raise HTTPException(400, "concurrency 必须在 1~32 之间")
    cfg = settings.load()
    cfg.check_concurrency = value
    settings.save(cfg)
    logger.info("check_concurrency set to %d", value)
    return {"concurrency": value}


# ============================================================
# 用量统计接口
# ============================================================
@router.get("/usage")
async def get_usage(days: int = 1, _=Depends(verify_admin)):
    """获取用量统计"""
    records = await read_usage(days)
    total = {"pt": 0, "ct": 0, "tt": 0, "requests": 0}
    by_day = {}
    by_model = {}
    
    for r in records:
        ts = r.get("ts", 0)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        pt = r.get("pt", 0) or 0
        ct = r.get("ct", 0) or 0
        tt = r.get("tt", 0) or (pt + ct)
        m = r.get("model", "unknown")
        p = r.get("provider", "unknown")
        total["pt"] += pt
        total["ct"] += ct
        total["tt"] += tt
        total["requests"] += 1
        d = by_day.setdefault(day, {"pt": 0, "ct": 0, "tt": 0, "requests": 0})
        d["pt"] += pt
        d["ct"] += ct
        d["tt"] += tt
        d["requests"] += 1
        mk = f"{p} · {m}"
        mm = by_model.setdefault(mk, {"pt": 0, "ct": 0, "tt": 0, "requests": 0, "provider": p, "model": m})
        mm["pt"] += pt
        mm["ct"] += ct
        mm["tt"] += tt
        mm["requests"] += 1
    
    by_day_list = [{"date": d, **v} for d, v in sorted(by_day.items())]
    by_model_list = [
        {"provider": v["provider"], "model": v["model"], "pt": v["pt"], "ct": v["ct"], "tt": v["tt"], "requests": v["requests"]}
        for _, v in sorted(by_model.items(), key=lambda x: -x[1]["tt"])
    ]
    return {"days": days, "total": total, "by_day": by_day_list, "by_model": by_model_list}


@router.get("/stability")
async def get_stability(hours: int = 24, _=Depends(verify_admin)):
    """获取稳定性统计"""
    from datetime import datetime, timedelta
    from services.usage_service import read_health_history
    from services.health_service import health_state, explain_status
    from database.models import Provider, SpeedRecord
    from database.engine import db
    records = await read_health_history(hours)
    # 已永久移除的模型不再出现在统计列表
    async with db.SessionLocal() as s:
        retired_provs = (await s.execute(select(Provider))).scalars().all()
    # 最近 7 天的真实测速结果（每个 provider+model 取最新一条）。
    # 单独一个 session + try：测速表缺失或查询失败不应该把整个监控页拖垮。
    try:
        async with db.SessionLocal() as s2:
            speed_rows = (await s2.execute(
                select(SpeedRecord)
                .where(SpeedRecord.tested_at >= datetime.now() - timedelta(days=7))
                .order_by(SpeedRecord.tested_at.desc())
                .limit(3000)
            )).scalars().all()
    except Exception as e:
        logger.warning("读取测速记录失败（speed_records 可能尚未创建）：%s", e)
        speed_rows = []
    retired_keys = {f"{p.name}||{m}" for p in retired_provs for m in (p.retired_models or [])}
    speed_map = {}
    for r in speed_rows:
        speed_map.setdefault(f"{r.provider_name}||{r.model}", r)
    
    model_stats = {}
    for rec in records:
        for key, info in rec.get("data", {}).items():
            if key in retired_keys:
                continue
            if key not in model_stats:
                model_stats[key] = {
                    "ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": [],
                }
            model_stats[key]["total"] += 1
            st = info.get("status", "unknown")
            if st == "ok":
                model_stats[key]["ok"] += 1
                if info.get("latency_ms"):
                    model_stats[key]["latencies"].append(info["latency_ms"])
            elif st == "fail":
                model_stats[key]["fail"] += 1
            elif st == "error":
                model_stats[key]["error"] += 1
    
    result = []
    for key, s in model_stats.items():
        name, model = key.split("||", 1)
        avg_lat = sum(s["latencies"]) / len(s["latencies"]) if s["latencies"] else None
        # 生成速度（tokens_per_sec）已下线：探针只生成 5 个 token、还夹着网络往返，
        # 算出来比实测慢 2~5 倍，早就不在表格里展示了。这个字段留着会让维护者
        # 以为它是可信的，所以连计算一起删掉——真实速度看下面的 speed_records。
        sp = speed_map.get(key)
        speed_ok = sp is not None and sp.status == "ok"
        # 解码速率要过物理校准：上游整包返回时 decode 窗口只有几毫秒，
        # 会算出几万 tok/s（历史数据里已经出现过 31750），这种数字不能当速度展示。
        from services.speed_service import decode_plausible
        decode_trusted = (
            speed_ok
            and sp.decode_tps is not None
            and decode_plausible(sp.output_tokens or 0, sp.decode_ms)
        )
        # 整包返回：解码速率测不出来，但端到端吞吐（输出 token ÷ 总耗时）仍有参考价值，
        # 前端用「≈」前缀标注，和真实 decode 速率区分开。
        speed_buffered = bool(
            speed_ok and not decode_trusted
            and (sp.overall_tps or 0) > 0 and (sp.output_tokens or 0) >= 8
        )
        result.append({
            "provider": name,
            "model": model,
            "checks": s["total"],
            "ok": s["ok"],
            "fail": s["fail"],
            "error": s["error"],
            "availability": round(s["ok"] / s["total"] * 100, 1) if s["total"] else 0,
            "avg_latency_ms": round(avg_lat) if avg_lat else None,
            "min_latency_ms": min(s["latencies"]) if s["latencies"] else None,
            "max_latency_ms": max(s["latencies"]) if s["latencies"] else None,
            # 真实测速结果（流式 128 token，手动触发才更新；未测过为 None）
            "ttft_ms": sp.ttft_ms if speed_ok else None,
            "real_tps": sp.decode_tps if decode_trusted else None,
            "speed_overall_tps": sp.overall_tps if speed_ok else None,
            "speed_buffered": speed_buffered,
            "speed_attempts": int(getattr(sp, "attempts", 1) or 1) if speed_ok else 1,
            "speed_estimated": bool(sp.estimated) if speed_ok else False,
            "speed_tested_at": sp.tested_at.timestamp() if sp is not None and hasattr(sp.tested_at, "timestamp") else None,
            "speed_error": ((sp.error or "")[:120] if sp is not None and not speed_ok else ""),
            "last_status": health_state.health_status.get(key, {}).get("status", "unknown"),
        })
        latest = health_state.health_status.get(key, {})
        # 内存里没有时，兜底取最近一条历史记录（含 code/detail）
        code = latest.get("code")
        detail = latest.get("detail", "")
        if code is None and not detail:
            for rec in records:
                hist = rec.get("data", {}).get(key, {})
                if hist.get("code") is not None or hist.get("detail"):
                    code = hist.get("code")
                    detail = hist.get("detail", "") or ""
                    break
        explain = explain_status(result[-1]["last_status"], code, detail)
        result[-1]["status_code"] = code
        result[-1]["status_reason"] = explain["label"]
        result[-1]["status_kind"] = explain["kind"]
        result[-1]["status_short"] = explain["short"]
    result.sort(key=lambda x: (-x["availability"], x["avg_latency_ms"] or 99999))
    return result


# ============================================================
# 历史接口
# ============================================================
@router.get("/history")
async def get_history(hours: int = 24, _=Depends(verify_admin)):
    """获取健康历史"""
    from services.usage_service import read_health_history
    return await read_health_history(hours)


@router.get("/check-log")
async def get_check_log(_=Depends(verify_admin)):
    """最近检查日志：两个固定槽位——最近一次单服务商检查 + 最近一次全量检查。

    每个槽位只保留最新一份，不含 API Key。
    """
    from sqlalchemy import select
    from database.models import CheckLog
    from database.engine import db

    async with db.SessionLocal() as session:
        rows = (await session.execute(select(CheckLog))).scalars().all()

    def pack(r: CheckLog) -> dict:
        return {
            "scope": r.scope or "",
            "checked_at": r.checked_at.timestamp() if hasattr(r.checked_at, "timestamp") else None,
            "count": len(r.items or []),
            "ok": r.ok_count,
            "fail": r.fail_count,
            "items": r.items or [],
        }

    out = {"provider": None, "all": None}
    for r in rows:
        if r.slot in out:
            out[r.slot] = pack(r)
    return out


# ============================================================
# 真实生成速度测速（流式，手动触发）
# ============================================================
class SpeedTestIn(BaseModel):
    items: Optional[list] = None      # [{provider, model}, ...]
    max_tokens: int = 128


@router.post("/speed-test/start")
async def start_speed_test(body: SpeedTestIn, _=Depends(verify_admin)):
    """启动一次真实生成速度测速。

    口径：测的是「前端当前表格里筛出来的这一列」，与「模型管理」里的勾选、
    提供商检测开关都无关——用户点的是「测我现在看到的这些」。只校验提供商仍然存在。
    任务在后台跑，前端用 /speed-test/status 轮询进度。
    """
    from database.models import Provider
    from database.engine import db
    from services import speed_service

    raw = body.items or []
    if not raw:
        raise HTTPException(400, "没有要测速的模型：请先筛选表格，再点测速")

    try:
        max_tokens = max(16, min(512, int(body.max_tokens or 128)))
    except (TypeError, ValueError):
        max_tokens = 128

    async with db.SessionLocal() as session:
        providers = {p.name: p for p in (await session.execute(select(Provider))).scalars().all()}

    picked: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for it in raw:
        if not isinstance(it, dict):
            skipped += 1
            continue
        name = str(it.get("provider") or "").strip()
        model = str(it.get("model") or "").strip()
        if not model or name not in providers:
            skipped += 1  # 提供商已被删除 / 数据不完整
            continue
        key = (name, model)
        if key in seen:
            continue
        seen.add(key)
        picked.append(key)

    if not picked:
        raise HTTPException(
            400,
            "表格里没有可测速的模型：这些模型所属的提供商可能已被删除。"
            "点「↻ 刷新」重新拉取列表后再试",
        )
    if len(picked) > speed_service.MAX_ITEMS:
        skipped += len(picked) - speed_service.MAX_ITEMS
        picked = picked[: speed_service.MAX_ITEMS]

    run_id = speed_service.create_run(picked, max_tokens, skipped)
    logger.info("speed test %s started: %d models, max_tokens=%d, skipped=%d",
                run_id, len(picked), max_tokens, skipped)
    return {
        "ok": True,
        "run_id": run_id,
        "total": len(picked),
        "skipped": skipped,
        "max_tokens": max_tokens,
    }


@router.get("/speed-test/status")
async def speed_test_status(run_id: str, _=Depends(verify_admin)):
    """查询测速进度。服务重启后内存任务会丢，返回 404。"""
    from services import speed_service

    run = speed_service.get_run(run_id)
    if not run:
        raise HTTPException(404, "测速任务不存在或已过期（服务重启会清空任务）")
    return run


# ============================================================
# 模型元数据接口
# ============================================================
@router.get("/model-details")
async def get_model_details(_=Depends(verify_admin)):
    """获取模型详情"""
    meta = ModelMetaService()
    merged = {}
    
    # 从数据库获取
    from database.models import ModelMeta
    from database.engine import db
    
    async with db.SessionLocal() as session:
        result = await session.execute(select(ModelMeta))
        for m in result.scalars().all():
            merged[m.model_id] = {
                "context_length": m.context_length,
                "desc": m.description,
                "rate_limit": m.rate_limit,
                "size": m.size,
            }
    
    # 合并 meta 数据
    for k, v in meta.model_descriptions.items():
        if k not in merged:
            merged[k] = {}
        if not merged[k].get("context_length"):
            merged[k]["context_length"] = v.get("ctx")
        merged[k]["desc"] = v.get("desc", "")
        if not merged[k].get("size"):
            merged[k]["size"] = v.get("size", "")
        rl = meta.rate_limits.get(k)
        if rl:
            merged[k]["rate_limit"] = rl
    
    return merged


@router.get("/context-limits")
async def get_context_limits(_=Depends(verify_admin)):
    """获取上下文长度配置"""
    return {"ok": True, "data": ModelMetaService().context_limits}


@router.put("/context-limits")
async def update_context_limit(req: ContextLimitUpdate, _=Depends(verify_admin)):
    """更新上下文长度"""
    meta = ModelMetaService()
    meta.set_context_limit(req.model, req.context_length)
    return {"ok": True}


@router.delete("/context-limits/{model}")
async def delete_context_limit(model: str, _=Depends(verify_admin)):
    """删除上下文长度配置"""
    meta = ModelMetaService()
    meta.delete_context_limit(model)
    return {"ok": True}


# ============================================================
# 速率限制接口
# ============================================================
class RateLimitsIn(BaseModel):
    limits: dict[str, str]


@router.post("/rate-limits")
async def save_rate_limits(body: RateLimitsIn, _=Depends(verify_admin)):
    """手动保存速率限制配置（真实生效）"""
    meta = ModelMetaService()
    meta.set_rate_limits(body.limits)
    return {"ok": True, "count": len(body.limits)}


@router.get("/rate-limits")
async def get_rate_limits(_=Depends(verify_admin)):
    """读取当前速率限制配置"""
    return {"ok": True, "limits": ModelMetaService().rate_limits}


@router.post("/sync-rate-limits")
async def sync_rate_limits_api(_=Depends(verify_admin)):
    """从公开数据源同步速率限制（不改变手动设置）"""
    async with httpx.AsyncClient(timeout=30, verify=settings.load().verify_ssl) as client:
        result = await sync_rate_limits(client, ModelMetaService())
    return result


# ============================================================
# 公告接口
# ============================================================
@router.get("/announcement")
async def get_announcement(_=Depends(verify_admin)):
    """获取系统公告"""
    from pathlib import Path
    import json
    cfg = settings.load()
    cache_file = Path(__file__).parent.parent / "announcement_cache.json"
    now = time.time()
    # 检查本地缓存是否有效（TTL 5分钟）
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if now - cached.get("ts", 0) < 300:
                return {"ok": True, "content": cached.get("content", "")}
        except Exception:
            pass
    # 尝试从远程获取
    url = cfg.announcement_url or "https://gitee.com/ywtc000/dongye/raw/master/announcement.md"
    try:
        async with httpx.AsyncClient(timeout=15, verify=settings.load().verify_ssl) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                content = resp.text.strip()
                cache_file.write_text(json.dumps({"content": content, "ts": now}, ensure_ascii=False), encoding="utf-8")
                return {"ok": True, "content": content}
    except Exception as e:
        logger.warning("announcement fetch failed: %s", e)
    return {"ok": True, "content": ""}
