"""管理接口路由"""
import time
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy import select
import httpx

from config.settings import settings
from services.health_service import health_state, check_model, fetch_models
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

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/api")


def verify_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """管理面板鉴权"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing credentials")
    cfg = settings.load()
    if credentials.credentials != cfg.local_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


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


@router.post("/health-status/clear-fail-counts")
async def clear_fail_counts(provider: Optional[str] = None, _=Depends(verify_admin)):
    """清除失败标记，可选只清除指定提供商的"""
    if provider:
        cleared = 0
        keys_to_remove = [k for k in health_state.circuit_breaker if k.startswith(provider + "||")]
        for k in keys_to_remove:
            health_state.clear_fail_count(k)
            cleared += 1
        return {"cleared": cleared}
    else:
        count = health_state.clear_all_fail_counts()
        return {"cleared": count}


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
    """获取上游可⽤模型列表（不保存）"""
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
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            models = await fetch_models(
                client, p.base_url, p.api_key, free_only=False,
                aliases=meta.aliases, context_limits=meta.context_limits,
            )
        if not models:
            return {"ok": False, "models": [], "detail": "上游模型列表获取失败。"}
        return {"ok": True, "models": models}
    except Exception as e:
        logger.warning("available-models fetch failed for %s: %s", name, e)
        return {"ok": False, "models": [], "detail": str(e)}


@router.post("/providers/{name}/fetch-models")
async def refresh_models(name: str, _=Depends(verify_admin)):
    """刷新模型列表"""
    svc = ProviderService()
    try:
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            models = await svc.fetch_available_models(
                name, client, ModelMetaService().aliases, ModelMetaService().context_limits
            )
        await svc.update(name, {"models": models, "disabled_models": []})
        return {"ok": True, "models": models}
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/providers/sync-free-models")
async def sync_all_free_models(_=Depends(verify_admin)):
    """批量同步免费模型"""
    svc = ProviderService()
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
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
    return {"ok": True, "data": await svc.list_all()}


@router.post("/routers")
async def save_routers(request: Request, _=Depends(verify_admin)):
    """保存路由组"""
    body = await request.json()
    svc = RouterService()
    await svc.save_all(body)
    return {"ok": True}


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
@router.post("/check/{name}/{model}")
async def manual_check(name: str, model: str, _=Depends(verify_admin)):
    """手动探测单个模型"""
    from database.models import Provider
    from database.engine import db
    
    async with db.SessionLocal() as session:
        result = await session.execute(
            Provider.__table__.select().where(Provider.name == name)
        )
        p = result.scalar_one_or_none()
        if not p:
            raise HTTPException(404, "未找到")
    
    meta = ModelMetaService()
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        check_result = await check_model(
            client, p.base_url, p.api_key, model, meta.aliases
        )
    key = f"{name}||{model}"
    health_state.health_status[key] = check_result
    return check_result


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
    
    # 启动后台检测任务
    import asyncio
    results_task = asyncio.create_task(run_full_check(on_progress=on_progress))
    
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
    global app_config
    cfg = settings.load()
    cfg.auto_validate = bool(body.enabled)
    settings.save(cfg)
    logger.info("auto_validate set to %s", cfg.auto_validate)
    return {
        "enabled": cfg.auto_validate,
        "interval": cfg.auto_validate_interval,
    }


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
    from services.usage_service import read_health_history
    records = await read_health_history(hours)
    
    model_stats = {}
    for rec in records:
        for key, info in rec.get("data", {}).items():
            if key not in model_stats:
                model_stats[key] = {"ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": []}
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
            "last_status": health_state.health_status.get(key, {}).get("status", "unknown"),
        })
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
        result = await session.execute(ModelMeta.__table__.select())
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
@router.post("/sync-rate-limits")
async def sync_rate_limits_api(_=Depends(verify_admin)):
    """同步速率限制"""
    async with httpx.AsyncClient(timeout=30, verify=False) as client:
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
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.get(url, follow_redirects=True)
            if resp.status_code == 200:
                content = resp.text.strip()
                cache_file.write_text(json.dumps({"content": content, "ts": now}, ensure_ascii=False), encoding="utf-8")
                return {"ok": True, "content": content}
    except Exception as e:
        logger.warning("announcement fetch failed: %s", e)
    return {"ok": True, "content": ""}
