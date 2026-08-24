"""Webhook 通知路由"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from config.settings import settings

router = APIRouter(prefix="/api/webhooks")
security = HTTPBearer(auto_error=False)


def verify_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if not credentials or credentials.credentials != settings.load().local_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


class WebhookConfig(BaseModel):
    url: str
    events: list[str] = ["model.failed", "model.recovered", "provider.error"]
    secret: str = ""


@router.get("/configs")
async def list_webhooks(_=Depends(verify_admin)):
    """列出所有 Webhook 配置"""
    # TODO: 从数据库读取
    return {"ok": True, "webhooks": []}


@router.post("/configs")
async def add_webhook(data: WebhookConfig, _=Depends(verify_admin)):
    """添加 Webhook"""
    # TODO: 保存到数据库
    return {"ok": True}


@router.delete("/configs/{id}")
async def delete_webhook(id: int, _=Depends(verify_admin)):
    """删除 Webhook"""
    # TODO: 从数据库删除
    return {"ok": True}


async def notify_webhooks(event: str, data: dict) -> None:
    """触发 Webhook 通知"""
    # TODO: 异步发送通知
    pass
