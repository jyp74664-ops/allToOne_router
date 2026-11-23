"""服务层：免费额度管家。

只做两件事：
1. 算出每家提供商「今日已用多少」——真实转发 + 探针都算，两者都消耗额度；
2. 给出「是否到顶」，检测流程据此自动跳过该家，避免白白撞 429/402。

配额由用户手填（上游不提供统一的配额查询接口），0 表示不限。
"""
import logging
from datetime import datetime

from sqlalchemy import select, func

from database.engine import db
from database.models import HealthRecord, Provider, ProviderQuota, UsageRecord

logger = logging.getLogger("flowgate.quota")


def _today_start() -> datetime:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)


async def today_usage() -> dict[str, dict]:
    """今日每家提供商的请求数与 token 数（转发 + 探针）。

    探针也是真实请求，同样吃免费额度；只统计转发会系统性低估。
    """
    start = _today_start()
    out: dict[str, dict] = {}
    async with db.SessionLocal() as session:
        usage_rows = (await session.execute(
            select(
                UsageRecord.provider,
                func.count(UsageRecord.id),
                func.sum(UsageRecord.prompt_tokens),
                func.sum(UsageRecord.completion_tokens),
            )
            .where(UsageRecord.timestamp >= start)
            .group_by(UsageRecord.provider)
        )).all()
        health_rows = (await session.execute(
            select(
                HealthRecord.provider_name,
                func.count(HealthRecord.id),
                func.sum(HealthRecord.prompt_tokens),
                func.sum(HealthRecord.completion_tokens),
            )
            .where(HealthRecord.checked_at >= start)
            .group_by(HealthRecord.provider_name)
        )).all()

    for name, count, pt, ct in usage_rows:
        item = out.setdefault(name, {"requests": 0, "tokens": 0})
        item["requests"] += int(count or 0)
        item["tokens"] += int(pt or 0) + int(ct or 0)
    for name, count, pt, ct in health_rows:
        item = out.setdefault(name, {"requests": 0, "tokens": 0})
        item["requests"] += int(count or 0)
        item["tokens"] += int(pt or 0) + int(ct or 0)
    return out


async def load_quotas() -> dict[str, dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(select(ProviderQuota))).scalars().all()
    return {
        r.provider_name: {
            "daily_requests": r.daily_requests or 0,
            "daily_tokens": r.daily_tokens or 0,
            "enabled": bool(r.enabled),
            "note": r.note or "",
        }
        for r in rows
    }


async def save_quota(
    provider_name: str,
    daily_requests: int = 0,
    daily_tokens: int = 0,
    enabled: bool = True,
    note: str = "",
) -> dict:
    """新增或更新一家的配额。负数一律归零。"""
    daily_requests = max(0, int(daily_requests or 0))
    daily_tokens = max(0, int(daily_tokens or 0))
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(ProviderQuota).where(ProviderQuota.provider_name == provider_name)
        )).scalar_one_or_none()
        if row is None:
            row = ProviderQuota(provider_name=provider_name)
            session.add(row)
        row.daily_requests = daily_requests
        row.daily_tokens = daily_tokens
        row.enabled = bool(enabled)
        row.note = note or ""
        row.updated_at = datetime.now()
        await session.commit()
    return {
        "provider_name": provider_name,
        "daily_requests": daily_requests,
        "daily_tokens": daily_tokens,
        "enabled": bool(enabled),
        "note": note or "",
    }


async def delete_quota(provider_name: str) -> bool:
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(ProviderQuota).where(ProviderQuota.provider_name == provider_name)
        )).scalar_one_or_none()
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
        return True


async def quota_status() -> list[dict]:
    """所有提供商的配额 + 今日用量 + 是否到顶（供页面展示）。"""
    async with db.SessionLocal() as session:
        providers = (await session.execute(select(Provider))).scalars().all()
    quotas = await load_quotas()
    usage = await today_usage()
    out = []
    for p in providers:
        q = quotas.get(p.name, {"daily_requests": 0, "daily_tokens": 0, "enabled": True, "note": ""})
        used = usage.get(p.name, {"requests": 0, "tokens": 0})
        req_limit = q["daily_requests"]
        tok_limit = q["daily_tokens"]
        req_hit = bool(req_limit and used["requests"] >= req_limit)
        tok_hit = bool(tok_limit and used["tokens"] >= tok_limit)
        exhausted = bool(q["enabled"] and (req_hit or tok_hit))
        out.append({
            "provider": p.name,
            "enabled": q["enabled"],
            "daily_requests": req_limit,
            "daily_tokens": tok_limit,
            "used_requests": used["requests"],
            "used_tokens": used["tokens"],
            "requests_left": max(0, req_limit - used["requests"]) if req_limit else None,
            "tokens_left": max(0, tok_limit - used["tokens"]) if tok_limit else None,
            "requests_pct": round(used["requests"] / req_limit * 100, 1) if req_limit else None,
            "tokens_pct": round(used["tokens"] / tok_limit * 100, 1) if tok_limit else None,
            "exhausted": exhausted,
            "note": q["note"],
        })
    out.sort(key=lambda x: (not x["exhausted"], -x["used_requests"]))
    return out


async def is_exhausted(provider_name: str) -> tuple[bool, str]:
    """检测前调用：返回 (是否到顶, 说明)。没有配置配额时永远 False。"""
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(ProviderQuota).where(ProviderQuota.provider_name == provider_name)
        )).scalar_one_or_none()
    if row is None or not row.enabled:
        return False, ""
    req_limit = row.daily_requests or 0
    tok_limit = row.daily_tokens or 0
    if not req_limit and not tok_limit:
        return False, ""
    used = (await today_usage()).get(provider_name, {"requests": 0, "tokens": 0})
    if req_limit and used["requests"] >= req_limit:
        return True, f"今日请求 {used['requests']}/{req_limit} 已达上限"
    if tok_limit and used["tokens"] >= tok_limit:
        return True, f"今日 token {used['tokens']}/{tok_limit} 已达上限"
    return False, ""
