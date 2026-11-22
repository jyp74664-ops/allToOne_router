"""核心功能回归：只读端点、指标、限流、路由组、Webhook。不含外网调用。"""
import asyncio
from api.metrics import prometheus_metrics
from services.rate_limit_service import (
    translate_rate_limit, rate_limit_tooltip,
)
from services.router_manager import RouterService
from services.meta_service import ModelMetaService
from database import engine as engine_mod
from database.models import Provider


def test_health_and_static(local_client):
    assert local_client.get("/health").json()["status"] == "ok"
    assert local_client.get("/static/main.js").status_code == 200
    assert local_client.get("/static/style.css").status_code == 200


def test_readonly_admin_endpoints(local_client):
    """所有只读管理端点应可用（仅测本机，不消耗上游配额）。"""
    eps = [
        "/api/providers", "/api/routers", "/api/routers/aliases",
        "/api/health-status", "/api/stability?hours=1", "/api/usage?days=1",
        "/api/history?hours=1", "/api/rate-limits", "/api/context-limits",
        "/api/auto-validate", "/api/merge-reasoning", "/api/webhooks/configs",
        "/api/client-key", "/api/model-details",
    ]
    for ep in eps:
        r = local_client.get(ep)
        assert r.status_code == 200, f"{ep} -> {r.status_code}"


def test_metrics_model_count_not_zero(initialized_db):
    """冷启动时 models_total 应反映配置总数（unknown），而非全 0。"""
    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(
                name="p1", base_url="https://x/v1", api_key="k",
                models=["m1", "m2"], free_only=True,
            ))
            await s.commit()
    asyncio.run(seed())

    text = asyncio.run(prometheus_metrics()).body.decode("utf-8")
    assert 'flowgate_models_total{status="unknown"} 2' in text
    assert 'flowgate_providers_total 1' in text


def test_rate_limit_translation():
    assert "RPM" in translate_rate_limit("60 RPM")
    assert "每分钟" in rate_limit_tooltip("60 RPM, 1000 TPM")
    assert "积分" in rate_limit_tooltip("credit-metered")


def test_router_crud(initialized_db):
    """路由组以数据库为唯一数据源，CRUD 闭环。"""
    svc = RouterService()
    asyncio.run(svc.create("测试组", ["a", "b"], alias="t"))
    assert "测试组" in asyncio.run(svc.list_all())
    assert asyncio.run(svc.get_aliases()).get("t") == "测试组"
    assert asyncio.run(svc.delete("测试组")) is True
    assert "测试组" not in asyncio.run(svc.list_all())


def test_webhook_crud_and_validation(local_client):
    r = local_client.post("/api/webhooks/configs", json={
        "url": "https://example.com/hook", "events": ["model.failed"], "secret": "s3cr3t",
    })
    assert r.status_code == 200
    wid = r.json()["webhook"]["id"]
    listed = local_client.get("/api/webhooks/configs").json()["webhooks"]
    assert listed[0]["has_secret"] is True
    assert listed[0]["secret"] == "****"
    assert local_client.delete(f"/api/webhooks/configs/{wid}").status_code == 200
    assert local_client.post("/api/webhooks/test", json={"url": "ftp://bad"}).status_code == 400
