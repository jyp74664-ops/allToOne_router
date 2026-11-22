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


def test_check_provider_route_not_shadowed(local_client, monkeypatch):
    """/check/provider/{name} 必须匹配具体路由，不能被 /check/{name}/{model} 吞掉。"""
    import asyncio
    from database.models import Provider
    from database import engine as engine_mod
    import api.admin as admin_mod
    import services.health_service as hs

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="agnes-test", base_url="https://x/v1", api_key="k",
                           models=["m1"], disabled_models=[]))
            await s.commit()
    asyncio.run(seed())

    # 用假的探测函数，避免任何真实上游请求
    async def fake_check(client, base_url, api_key, model, aliases, **kw):
        return {"status": "ok", "code": 200, "latency_ms": 1}
    monkeypatch.setattr(admin_mod, "check_model", fake_check)
    monkeypatch.setattr(hs, "is_model_rate_limited", lambda k: False, raising=False)

    r = local_client.post("/api/check/provider/agnes-test")
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert "\"done\":true" in r.text.replace(" ", "")


def test_explain_status_real_meanings():
    """404/410/429 必须翻译成正确的人话，供前端图标展示。"""
    from services.health_service import explain_status

    not_open = explain_status("deleted", 404,
        "Function 'ee47' Not found for account 'NSo'")
    assert not_open["kind"] == "not_enabled"
    assert "未开通" in not_open["label"]

    eol = explain_status("fail", 410,
        "The model 'google/gemma-2b' has reached its end of life and is no longer available.")
    assert eol["kind"] == "offline"
    assert "下线" in eol["label"]

    limited = explain_status("rate_limited", 429, "rate limit exceeded")
    assert limited["kind"] == "rate_limited"

    ok = explain_status("ok", 200, "")
    assert ok["kind"] == "ok"

    auth = explain_status("fail", 403, "invalid api key")
    assert auth["kind"] == "no_auth"


def test_check_log_endpoint(local_client, initialized_db):
    """/api/check-log 返回最近检查明细，且带 reason。"""
    import asyncio
    from datetime import datetime
    from database import engine as engine_mod
    from database.models import HealthRecord

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(HealthRecord(
                provider_name="nvidia", model="google/gemma-2b",
                status="fail", code=410, latency_ms=100,
                detail="end of life", checked_at=datetime.now(),
            ))
            s.add(HealthRecord(
                provider_name="agnes", model="agnes-3.0-flash",
                status="ok", code=200, latency_ms=300,
                checked_at=datetime.now(),
            ))
            await s.commit()
    asyncio.run(seed())

    r = local_client.get("/api/check-log")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] >= 2
    by_model = {i["model"]: i for i in d["items"]}
    assert by_model["google/gemma-2b"]["kind"] == "offline"
    assert "下线" in by_model["google/gemma-2b"]["reason"]
    assert by_model["agnes-3.0-flash"]["kind"] == "ok"
    assert "api_key" not in r.text.lower()
