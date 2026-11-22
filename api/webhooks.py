"""Webhook 通知路由"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from api.auth import verify_admin
from config.settings import settings
from database.engine import db
from database.models import Webhook

logger = logging.getLogger("flowgate.webhooks")

router = APIRouter(prefix="/api/webhooks")


class WebhookConfig(BaseModel):
    url: str
    events: list[str] = ["model.failed", "model.recovered", "provider.error"]
    secret: str = ""
    enabled: bool = True


def _to_dict(w: Webhook) -> dict:
    return {
        "id": w.id,
        "url": w.url,
        "events": w.events or [],
        "secret": "****" if w.secret else "",
        "has_secret": bool(w.secret),
        "enabled": w.enabled,
    }


class WebhookTestIn(BaseModel):
    url: str
    secret: str = ""


@router.get("/configs")
async def list_webhooks(_=Depends(verify_admin)):
    """列出所有 Webhook 配置"""
    async with db.SessionLocal() as session:
        result = await session.execute(select(Webhook).order_by(Webhook.id))
        webhooks = [_to_dict(w) for w in result.scalars().all()]
    return {"ok": True, "webhooks": webhooks}


@router.post("/configs")
async def add_webhook(data: WebhookConfig, _=Depends(verify_admin)):
    """添加 Webhook"""
    async with db.SessionLocal() as session:
        w = Webhook(
            url=data.url,
            events=data.events,
            secret=data.secret,
            enabled=data.enabled,
        )
        session.add(w)
        await session.commit()
        await session.refresh(w)
        return {"ok": True, "webhook": _to_dict(w)}


@router.post("/test")
async def test_webhook(data: WebhookTestIn, _=Depends(verify_admin)):
    """发送一条测试事件，验证 Webhook 连通性（不落库）。"""
    from urllib.parse import urlparse
    parsed = urlparse(data.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "Webhook URL 必须是有效的 http(s) 地址")

    import httpx
    headers = {"Content-Type": "application/json"}
    if data.secret:
        headers["X-Webhook-Secret"] = data.secret
    payload = {
        "event": "webhook.test",
        "data": {"message": "FlowGate webhook 连通性测试", "ts": int(__import__("time").time())},
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=settings.load().verify_ssl) as client:
            resp = await client.post(data.url, json=payload, headers=headers)
        return {
            "ok": 200 <= resp.status_code < 300,
            "status_code": resp.status_code,
            "detail": resp.text[:200],
        }
    except Exception as e:
        return {"ok": False, "status_code": 0, "detail": str(e)[:200]}


@router.delete("/configs/{id}")
async def delete_webhook(id: int, _=Depends(verify_admin)):
    """删除 Webhook"""
    async with db.SessionLocal() as session:
        w = await session.get(Webhook, id)
        if not w:
            raise HTTPException(404, "未找到该 Webhook")
        await session.delete(w)
        await session.commit()
    return {"ok": True}


async def notify_webhooks(event: str, data: dict) -> None:
    """触发 Webhook 通知（异步发送，失败不抛异常）"""
    import httpx

    async with db.SessionLocal() as session:
        result = await session.execute(
            select(Webhook).where(Webhook.enabled == True)  # noqa: E712
        )
        webhooks = result.scalars().all()

    if not webhooks:
        return

    payload = {"event": event, "data": data}
    async with httpx.AsyncClient(timeout=10, verify=settings.load().verify_ssl) as client:
        for w in webhooks:
            if w.events and event not in w.events:
                continue
            headers = {"Content-Type": "application/json"}
            if w.secret:
                headers["X-Webhook-Secret"] = w.secret
            try:
                await client.post(w.url, json=payload, headers=headers)
            except Exception as e:
                logger.warning("webhook %s notify failed: %s", w.url, e)
