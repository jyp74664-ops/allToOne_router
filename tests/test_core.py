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


def test_check_log_two_slots(local_client, initialized_db):
    """/api/check-log 返回两个槽位；同槽位再写入会覆盖，只留最新一份。"""
    import asyncio
    from services.usage_service import save_check_log

    first = {"nvidia||google/gemma-2b": {"status": "eol", "code": 410, "detail": "end of life"}}
    second = {"agnes||agnes-3.0-flash": {"status": "ok", "code": 200, "latency_ms": 300}}

    asyncio.run(save_check_log("provider", first, scope="nvidia"))
    asyncio.run(save_check_log("all", first))

    r = local_client.get("/api/check-log")
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d.keys()) == {"provider", "all"}
    assert d["provider"]["scope"] == "nvidia"
    assert d["provider"]["count"] == 1
    assert d["provider"]["items"][0]["kind"] == "offline"
    assert d["all"]["count"] == 1

    # 再写一次 provider 槽：必须覆盖，且全量槽不受影响
    asyncio.run(save_check_log("provider", second, scope="agnes"))
    d2 = local_client.get("/api/check-log").json()
    assert d2["provider"]["scope"] == "agnes"
    assert d2["provider"]["items"][0]["model"] == "agnes-3.0-flash"
    assert d2["all"]["items"][0]["model"] == "google/gemma-2b"
    assert "api_key" not in r.text.lower()


def test_retire_models_removes_eol_and_404(initialized_db):
    """检测确认 EOL/404 后，模型从启用+限额列表移除并进入退役名单。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    from services.health_service import retire_models

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="retire-test", base_url="https://x/v1", api_key="k",
                           models=["good", "eol-model"],
                           disabled_models=["not-open"], retired_models=[]))
            await s.commit()
    asyncio.run(seed())

    removed = asyncio.run(retire_models("retire-test", ["eol-model", "not-open"]))
    assert set(removed) == {"eol-model", "not-open"}

    async def verify():
        async with db.SessionLocal() as s:
            from sqlalchemy import select
            p_ = (await s.execute(select(Provider).where(Provider.name == "retire-test"))).scalar_one()
            assert p_.models == ["good"]
            assert p_.disabled_models == []
            assert set(p_.retired_models) == {"eol-model", "not-open"}
    asyncio.run(verify())


def test_explain_status_eol():
    """EOL 状态应显示为已下线并标注会自动移除。"""
    from services.health_service import explain_status
    ex = explain_status("eol", 410, "end of life")
    assert ex["kind"] == "offline"
    assert "移除" in ex["label"]


def test_sync_skips_retired_models(initialized_db, monkeypatch):
    """同步免费模型时不会把已退役的模型加回来。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.provider_service as ps

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="sync-test", base_url="https://x/v1", api_key="k",
                           models=[], disabled_models=[],
                           retired_models=["dead-model"]))
            await s.commit()
    asyncio.run(seed())

    async def fake_fetch(*a, **kw):
        return ["dead-model", "live-model"]
    monkeypatch.setattr(ps, "_fetch_models", fake_fetch)

    svc = ps.ProviderService()
    import httpx
    results = asyncio.run(svc.sync_free_models(
        httpx.AsyncClient(), {}, {}, {}
    ))
    r = next(x for x in results if x["name"] == "sync-test")
    assert r["removed_retired"] >= 1

    async def verify():
        async with db.SessionLocal() as s:
            from sqlalchemy import select
            p_ = (await s.execute(select(Provider).where(Provider.name == "sync-test"))).scalar_one()
            assert p_.models == ["live-model"]
    asyncio.run(verify())


def test_check_enabled_skips_provider(initialized_db, monkeypatch):
    """check_enabled=False 的提供商不参与全量检测。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.health_service as hs

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="on-prov", base_url="https://a/v1", api_key="k",
                           models=["m-on"], disabled_models=[], check_enabled=True))
            s.add(Provider(name="off-prov", base_url="https://b/v1", api_key="k",
                           models=["m-off"], disabled_models=[], check_enabled=False))
            await s.commit()
    asyncio.run(seed())

    called = []

    async def fake_check(client, base_url, api_key, model, aliases, **kw):
        called.append(model)
        return {"status": "ok", "code": 200, "latency_ms": 1}
    monkeypatch.setattr(hs, "check_model", fake_check)

    results = asyncio.run(hs.run_full_check())
    assert "m-on" in called
    assert "m-off" not in called
    assert any(k.endswith("||m-on") for k in results)
    assert not any(k.endswith("||m-off") for k in results)


def test_check_provider_rejects_disabled(local_client, initialized_db):
    """关闭检测后手动单点检查返回 400。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="closed", base_url="https://x/v1", api_key="k",
                           models=["m"], disabled_models=[], check_enabled=False))
            await s.commit()
    asyncio.run(seed())

    r = local_client.post("/api/check/provider/closed")
    assert r.status_code == 400, r.text
    assert "关闭检测" in r.text


def test_providers_list_exposes_check_enabled(local_client, initialized_db):
    """列表接口返回 check_enabled，供前端渲染开关。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="p-on", base_url="https://a/v1", api_key="k",
                           models=["m"], disabled_models=[], check_enabled=True))
            s.add(Provider(name="p-off", base_url="https://b/v1", api_key="k",
                           models=["m"], disabled_models=[], check_enabled=False))
            await s.commit()
    asyncio.run(seed())

    r = local_client.get("/api/providers")
    assert r.status_code == 200, r.text
    by_name = {p["name"]: p for p in r.json()}
    assert by_name["p-on"]["check_enabled"] is True
    assert by_name["p-off"]["check_enabled"] is False


def test_full_check_skips_unchecked_models(initialized_db, monkeypatch):
    """立即检测只探测已勾选（models）的模型，未勾选（disabled_models）不探测。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.health_service as hs

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="mix", base_url="https://a/v1", api_key="k",
                           models=["m-on"], disabled_models=["m-off"],
                           retired_models=[], check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    called = []

    async def fake_check(client, base_url, api_key, model, aliases, **kw):
        called.append(model)
        return {"status": "ok", "code": 200, "latency_ms": 1}
    monkeypatch.setattr(hs, "check_model", fake_check)

    results = asyncio.run(hs.run_full_check())
    assert called == ["m-on"], called
    assert list(results) == ["mix||m-on"], list(results)


def test_checkable_models_respects_free_only():
    """勾了「仅拉取免费模型」只探测已知免费模型；未勾选则全测；免费集合未知时退回全测。"""
    from services.health_service import checkable_models

    class P:
        pass

    p = P()
    p.models = ["free-a", "paid-b"]
    p.free_models = ["free-a"]
    p.free_only = True
    assert checkable_models(p) == ["free-a"]

    p.free_only = False
    assert checkable_models(p) == ["free-a", "paid-b"]

    # 免费集合为空（从没同步过）时无法区分收费，不能把整家跳过
    p.free_only = True
    p.free_models = []
    assert checkable_models(p) == ["free-a", "paid-b"]


def test_full_check_skips_paid_models_when_free_only(initialized_db, monkeypatch):
    """立即检测：free_only 的提供商只探测免费模型，收费模型不碰。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.health_service as hs

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="freeonly", base_url="https://a/v1", api_key="k",
                           models=["free-a", "paid-b"], disabled_models=[],
                           retired_models=[], free_models=["free-a"],
                           free_only=True, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    called = []

    async def fake_check(client, base_url, api_key, model, aliases, **kw):
        called.append(model)
        return {"status": "ok", "code": 200, "latency_ms": 1}
    monkeypatch.setattr(hs, "check_model", fake_check)

    asyncio.run(hs.run_full_check())
    assert called == ["free-a"], called


def test_manual_check_rejects_paid_model_when_free_only(local_client, initialized_db):
    """单模型检查：free_only 提供商下的收费模型返回 400，不发探测请求。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="fo", base_url="https://a/v1", api_key="k",
                           models=["free-a", "paid-b"], disabled_models=[],
                           retired_models=[], free_models=["free-a"],
                           free_only=True, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    r = local_client.post("/api/check/fo/paid-b")
    assert r.status_code == 400, r.text
    assert "收费模型" in r.text
