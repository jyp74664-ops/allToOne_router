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


def test_manual_check_allows_paid_model_when_free_off(local_client, initialized_db, monkeypatch):
    """显示「含收费」的提供商（free_only=False）单模型检查不能被 400 拦掉。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import api.admin as admin_mod

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="paidok", base_url="https://a/v1", api_key="k",
                           models=["free-a", "paid-b"], disabled_models=[],
                           retired_models=[], free_models=["free-a"],
                           free_only=False, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    async def fake_check(client=None, base_url=None, api_key=None, model=None, aliases=None, **kw):
        return {"status": "ok", "code": 200, "latency_ms": 1}
    monkeypatch.setattr(admin_mod, "check_model", fake_check)

    r = local_client.post("/api/check/paidok/paid-b")
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "ok"


def test_model_pricing_is_paid_handles_nested_providers():
    """AMD/OpenRouter 这类把价格放在 providers[].pricing 里的上游也要能判出收费。"""
    from services.health_service import model_pricing_is_paid

    # 扁平：全 0 → 免费
    assert model_pricing_is_paid({"pricing": {"prompt": "0", "completion": "0"}}) is False
    # 扁平：非 0 → 收费
    assert model_pricing_is_paid({"pricing": {"prompt": "3.5e-8", "completion": "0"}}) is True
    # 嵌套：所有渠道都收费 → 收费
    assert model_pricing_is_paid({"providers": [
        {"pricing": {"prompt": "1e-6", "completion": "1e-6"}},
        {"pricing": {"prompt": "2e-6", "completion": "0"}},
    ]}) is True
    # 嵌套：有一个渠道免费 → 免费
    assert model_pricing_is_paid({"providers": [
        {"pricing": {"prompt": "1e-6", "completion": "1e-6"}},
        {"pricing": {"prompt": "0", "completion": "0"}},
    ]}) is False
    # 完全没有价格信息 → 按免费处理
    assert model_pricing_is_paid({"id": "x"}) is False


def test_check_provider_rejects_empty_models(local_client, initialized_db):
    """模型列表为空时不能静默返回 0/0，要给出可操作的 400 提示。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="empty", base_url="https://a/v1", api_key="k",
                           models=[], disabled_models=[], retired_models=[],
                           free_models=[], free_only=False, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    r = local_client.post("/api/check/provider/empty")
    assert r.status_code == 400, r.text
    assert "模型列表为空" in r.text


def test_stability_returns_tokens_per_sec(local_client, initialized_db):
    """稳定性接口按 completion_token ÷ 耗时给出生成速度。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import HealthRecord

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(HealthRecord(provider_name="p", model="m", status="ok", code=200,
                               latency_ms=1000, prompt_tokens=10,
                               completion_tokens=5, detail=""))
            await s.commit()
    asyncio.run(seed())

    r = local_client.get("/api/stability?hours=24")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["provider"] == "p" and x["model"] == "m")
    assert row["tokens_per_sec"] == 5.0


def test_sync_keeps_models_when_fetch_fails(initialized_db, monkeypatch):
    """上游拉取失败（返回 None）时同步不能清空已有模型列表。

    2026-11-23 事故：sync_free_models 直接把空列表写库，上游超时一次就把
    7 家提供商的模型清单全清掉了，检测随之变成 0 正常 / 0 异常。
    """
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.provider_service as ps

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="fail-fetch", base_url="https://x/v1", api_key="k",
                           models=["keep-a", "keep-b"], disabled_models=[], free_models=[]))
            await s.commit()
    asyncio.run(seed())

    async def fake_fetch(*a, **kw):
        return None
    monkeypatch.setattr(ps, "_fetch_models", fake_fetch)

    import httpx
    results = asyncio.run(ps.ProviderService().sync_free_models(
        httpx.AsyncClient(), {}, {}, {}
    ))
    r = next(x for x in results if x["name"] == "fail-fetch")
    assert r["ok"] is False
    assert r["count"] == 2

    async def verify():
        async with db.SessionLocal() as s:
            from sqlalchemy import select
            p_ = (await s.execute(select(Provider).where(Provider.name == "fail-fetch"))).scalar_one()
            assert p_.models == ["keep-a", "keep-b"]
    asyncio.run(verify())


def test_sync_keeps_models_when_upstream_returns_empty(initialized_db, monkeypatch):
    """上游返回空列表（全收费/格式变化）时保留原配置，而不是写空列表。"""
    import asyncio
    from database import engine as engine_mod
    from database.models import Provider
    import services.provider_service as ps

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="empty-fetch", base_url="https://x/v1", api_key="k",
                           models=["keep-c"], disabled_models=[], free_models=[]))
            await s.commit()
    asyncio.run(seed())

    async def fake_fetch(*a, **kw):
        return []
    monkeypatch.setattr(ps, "_fetch_models", fake_fetch)

    import httpx
    results = asyncio.run(ps.ProviderService().sync_free_models(
        httpx.AsyncClient(), {}, {}, {}
    ))
    r = next(x for x in results if x["name"] == "empty-fetch")
    assert r["ok"] is False

    async def verify():
        async with db.SessionLocal() as s:
            from sqlalchemy import select
            p_ = (await s.execute(select(Provider).where(Provider.name == "empty-fetch"))).scalar_one()
            assert p_.models == ["keep-c"]
    asyncio.run(verify())


def test_pricing_is_free_prefers_is_free_flag():
    """上游明确 isFree=true 时优先采信，不被 -1 之类的价格字段干扰。"""
    from services.health_service import model_pricing_is_paid

    assert model_pricing_is_paid({"id": "a:free", "isFree": True,
                                  "pricing": {"prompt": "0", "completion": "0"}}) is False
    # -1 表示价格未知/动态路由，按收费处理，避免把付费自动路由拉进免费名单
    assert model_pricing_is_paid({"id": "auto", "isFree": False,
                                  "pricing": {"prompt": "-1", "completion": "-1"}}) is True
    assert model_pricing_is_paid({"id": "paid", "pricing": {"prompt": "0.000002",
                                                           "completion": "0.000004"}}) is True
    assert model_pricing_is_paid({"id": "no-pricing"}) is False


def test_speed_test_uses_current_view(initialized_db, local_client, monkeypatch):
    """测速按「当前视图」走：未勾选、甚至已关检测的模型，只要在表格里就会被测。

    2026-11-23 用户改口径：原来只测 provider.models 里勾选的，实际用起来大量行被跳过。
    """
    from database import engine as engine_mod
    import services.speed_service as speed_service

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="sp-view", base_url="https://x/v1", api_key="k",
                           models=["checked-a"], disabled_models=["unchecked-b"],
                           retired_models=[], free_models=[], free_only=False,
                           check_enabled=False))
            await s.commit()
    asyncio.run(seed())

    captured = {}

    def fake_create_run(items, max_tokens, skipped=0):
        captured["items"] = list(items)
        captured["skipped"] = skipped
        return "run-test"

    monkeypatch.setattr(speed_service, "create_run", fake_create_run)

    r = local_client.post(
        "/api/speed-test/start",
        json={"items": [{"provider": "sp-view", "model": "unchecked-b"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert captured["items"] == [("sp-view", "unchecked-b")]
    assert captured["skipped"] == 0


def test_speed_test_rejects_unknown_provider(initialized_db, local_client):
    """提供商已被删除时没有可测对象，返回 400 而不是静默返回空任务。"""
    r = local_client.post(
        "/api/speed-test/start",
        json={"items": [{"provider": "nope", "model": "m"}]},
    )
    assert r.status_code == 400, r.text


def test_stability_exposes_real_speed(initialized_db, local_client):
    """真实测速结果要出现在 /api/stability 的 ttft_ms / real_tps 上。

    HealthRecord（探针）与 SpeedRecord（测速）是两套数据，不能互相污染。
    """
    from datetime import datetime
    from database import engine as engine_mod
    from database.models import HealthRecord, SpeedRecord

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(HealthRecord(provider_name="p2", model="m2", status="ok", code=200,
                               latency_ms=500, prompt_tokens=5, completion_tokens=5,
                               detail=""))
            s.add(SpeedRecord(run_id="r1", provider_name="p2", model="m2", status="ok",
                              ttft_ms=350, decode_tps=42.5, overall_tps=30.1,
                              output_tokens=128, decode_ms=3000, wall_ms=3350,
                              estimated=False, tested_at=datetime.now()))
            await s.commit()
    asyncio.run(seed())

    r = local_client.get("/api/stability?hours=24")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["provider"] == "p2" and x["model"] == "m2")
    assert row["ttft_ms"] == 350
    assert row["real_tps"] == 42.5
    assert row["speed_estimated"] is False
    assert row["speed_tested_at"] is not None
    # 探针口径的旧字段仍然存在，只是前端不再展示
    assert row["tokens_per_sec"] == 10.0


def test_decode_plausible_rejects_buffered_streams():
    """整包返回时首个/末个 delta 只差几毫秒，算出的几万 tok/s 必须被判为不可信。"""
    from services.speed_service import decode_plausible, MAX_PLAUSIBLE_TPS

    # 真实事故数据：128 token 只用了 4ms（≈31750 tok/s）
    assert decode_plausible(128, 4) is False
    # 6 个 token 用了 1ms（≈5000 tok/s）
    assert decode_plausible(6, 1) is False
    # 正常逐 token 生成：128 token / 3 秒
    assert decode_plausible(128, 3000) is True
    # 极快但仍在物理上限内：按上限本身应当通过
    assert decode_plausible(MAX_PLAUSIBLE_TPS + 1, 1000) is True
    # 数据缺失一律不可信
    assert decode_plausible(0, 100) is False
    assert decode_plausible(128, None) is False


def _fake_speed_result(tokens=128, tps=600.0, ttft=500):
    """按目标 tok/s 反推 decode_ms，保证 decode_plausible 的判断和真实代码一致。"""
    decode_ms = max(1, round((tokens - 1) / tps * 1000))
    return {
        "status": "ok", "code": 200, "ttft_ms": ttft, "decode_ms": decode_ms,
        "wall_ms": ttft + decode_ms, "output_tokens": tokens, "chunks": tokens,
        "decode_tps": round((tokens - 1) / (decode_ms / 1000), 2),
        "overall_tps": round(tokens / ((ttft + decode_ms) / 1000), 2),
        "estimated": False, "buffered": False,
    }


def test_speed_retest_above_500_and_average(monkeypatch):
    """首测 >500 tok/s 时补测两次，取三次（可信样本）的平均值。"""
    from services import speed_service

    calls = []
    script = [_fake_speed_result(tps=600), _fake_speed_result(tps=700), _fake_speed_result(tps=800)]

    async def fake_stream_once(*args, **kwargs):
        calls.append(kwargs)
        return script[len(calls) - 1]

    monkeypatch.setattr(speed_service, "_stream_once", fake_stream_once)
    res = asyncio.run(speed_service.measure_stream_speed(None, "https://x/v1", "k", "m"))

    assert len(calls) == 3, "超过 500 tok/s 必须补测两次"
    assert res["attempts"] == 3
    assert res["averaged"] is True
    assert res["buffered"] is False
    # 600/700/800 三次的 decode_tps 均值
    assert 680 < res["decode_tps"] < 720, res["decode_tps"]


def test_speed_no_retest_below_500(monkeypatch):
    """没超过 500 tok/s 就只测一次，不浪费额度。"""
    from services import speed_service

    calls = []

    async def fake_stream_once(*args, **kwargs):
        calls.append(1)
        return _fake_speed_result(tps=300)

    monkeypatch.setattr(speed_service, "_stream_once", fake_stream_once)
    res = asyncio.run(speed_service.measure_stream_speed(None, "https://x/v1", "k", "m"))

    assert len(calls) == 1
    assert res["attempts"] == 1
    assert res["averaged"] is False
    assert 290 < res["decode_tps"] < 310


def test_speed_retest_all_buffered_stays_buffered(monkeypatch):
    """三次都是整包返回（几万 tok/s）时，平均也救不回来，只能标 buffered。"""
    from services import speed_service

    calls = []
    script = [_fake_speed_result(tps=31750), _fake_speed_result(tps=25000), _fake_speed_result(tps=40000)]

    async def fake_stream_once(*args, **kwargs):
        calls.append(1)
        return script[len(calls) - 1]

    monkeypatch.setattr(speed_service, "_stream_once", fake_stream_once)
    res = asyncio.run(speed_service.measure_stream_speed(None, "https://x/v1", "k", "m"))

    assert len(calls) == 3
    assert res["attempts"] == 3
    assert res["buffered"] is True
    assert res["decode_tps"] is None
    assert res["overall_tps"] is not None


def test_stability_hides_buffered_decode_speed(initialized_db, local_client):
    """上游整包返回的测速记录：real_tps 置空、标记 buffered，只留端到端吞吐。"""
    from datetime import datetime
    from database import engine as engine_mod
    from database.models import HealthRecord, SpeedRecord

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(HealthRecord(provider_name="p3", model="m3", status="ok", code=200,
                               latency_ms=1253, prompt_tokens=5, completion_tokens=5,
                               detail=""))
            # 复刻历史里那条 31750 tok/s：128 token、decode 窗口仅 4ms
            s.add(SpeedRecord(run_id="r3", provider_name="p3", model="m3", status="ok",
                              ttft_ms=2271, decode_tps=31750.0, overall_tps=56.26,
                              output_tokens=128, decode_ms=4, wall_ms=2275,
                              estimated=False, tested_at=datetime.now()))
            await s.commit()
    asyncio.run(seed())

    r = local_client.get("/api/stability?hours=24")
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["provider"] == "p3" and x["model"] == "m3")
    assert row["real_tps"] is None, "整包返回的 decode 速率不能当速度展示"
    assert row["speed_buffered"] is True
    assert row["speed_overall_tps"] == 56.26
    assert row["ttft_ms"] == 2271


def test_speed_test_status_unknown_run(local_client):
    """查询不存在的测速任务应返回 404，而不是空对象。"""
    r = local_client.get("/api/speed-test/status?run_id=nope")
    assert r.status_code == 404


# ============================================================
# 审计修复回归（2026-11-23 第一批）
# ============================================================

def _mock_http(handler):
    """造一个只走内存的 AsyncClient。"""
    import httpx
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_check_model_rejects_200_with_error_body():
    """HTTP 200 但响应体是 error：不能判 ok（审计 #1，已复现的那条）。"""
    import httpx
    from services.health_service import check_model

    def handler(request):
        return httpx.Response(200, json={"error": {"message": "insufficient quota"}})

    res = asyncio.run(check_model(_mock_http(handler), "https://x/v1", "k", "m", {}))
    assert res["status"] == "fail", res
    assert "quota" in res["detail"]


def test_check_model_rejects_200_without_choices():
    """200 但结构不是一次正常补全（没有 choices），同样不能判 ok。"""
    import httpx
    from services.health_service import check_model

    def handler(request):
        return httpx.Response(200, json={"status": "ok"})

    res = asyncio.run(check_model(_mock_http(handler), "https://x/v1", "k", "m", {}))
    assert res["status"] == "fail", res
    assert "choices" in res["detail"]


def test_check_model_accepts_normal_completion():
    """正常补全（有 choices、无 error）仍要判 ok，别把好的误杀。"""
    import httpx
    from services.health_service import check_model

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    res = asyncio.run(check_model(_mock_http(handler), "https://x/v1", "k", "m", {}))
    assert res["status"] == "ok", res
    assert res["completion_tokens"] == 1


def test_fetch_models_retries_with_auth_when_anonymous_empty():
    """匿名请求 200 但列表为空时必须再试带 Key 的那次（审计 #5，已复现）。"""
    import httpx
    from services.health_service import fetch_models

    seen = []

    def handler(request):
        auth = request.headers.get("authorization")
        seen.append(auth)
        if auth:
            return httpx.Response(200, json={"data": [{"id": "m-a"}, {"id": "m-b"}]})
        return httpx.Response(200, json={"data": []})

    models = asyncio.run(fetch_models(_mock_http(handler), "https://x/v1", "k", False, {}, {}, None))
    assert models == ["m-a", "m-b"], models
    assert any(a and a.startswith("Bearer") for a in seen), "必须真的试过认证请求"


def test_fetch_models_200_with_error_is_failure_not_empty():
    """200 包 error 属于拉取失败，要返回 None（保住原配置），不能当成空列表。"""
    import httpx
    from services.health_service import fetch_models

    def handler(request):
        return httpx.Response(200, json={"error": {"message": "invalid api key"}})

    res = asyncio.run(fetch_models(_mock_http(handler), "https://x/v1", "k", False, {}, {}, None))
    assert res is None, res


def test_db_runs_in_wal_mode(initialized_db):
    """库应跑在 WAL 模式，避免检测批量写与代理写用量互相锁库（审计 #9）。"""
    import sqlite3
    from database import engine as engine_mod

    conn = sqlite3.connect(engine_mod.db.db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal", mode


def _proxy_test_client(handler, provider_rows):
    """起一个 TestClient，并把 api.proxy 里的 httpx 换成内存 transport。"""
    import types
    import httpx
    from fastapi.testclient import TestClient
    import app
    from api import proxy as proxy_mod
    from database import engine as engine_mod
    from config.settings import settings

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            for row in provider_rows:
                s.add(Provider(**row))
            await s.commit()
    asyncio.run(seed())

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    shim = types.SimpleNamespace(
        AsyncClient=fake_client, Timeout=httpx.Timeout,
        HTTPError=httpx.HTTPError, MockTransport=httpx.MockTransport,
    )
    proxy_mod.httpx = shim

    c = TestClient(app.app, client=("127.0.0.1", 50002))
    c.__enter__()
    key = settings.load().local_api_key
    return c, key


def test_proxy_falls_back_when_upstream_returns_200_with_error(initialized_db, monkeypatch):
    """上游 200 包 error 时换下一个候选，而不是把错误直接抛给客户端（审计 #3）。"""
    import httpx
    from api import proxy as proxy_mod
    from services.health_service import health_state

    # 让 p-bad 排在前面，才能真正走到 fallback
    monkeypatch.setattr(health_state, "get_quality_score",
                        lambda key: 2.0 if key.startswith("p-bad") else 1.0)

    def handler(request):
        if request.url.host == "bad":
            return httpx.Response(200, json={"error": {"message": "insufficient quota"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "p-bad", "base_url": "https://bad/v1", "api_key": "k1", "models": ["m1"]},
        {"name": "p-good", "base_url": "https://good/v1", "api_key": "k2", "models": ["m1"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer " + key})
    finally:
        c.__exit__(None, None, None)
        proxy_mod.httpx = httpx

    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "ok"


def test_proxy_stream_429_puts_provider_in_cooldown(initialized_db):
    """流式请求撞 429 也要记 provider 冷却（审计 #4，原来只有非流式记）。"""
    import httpx
    from api import proxy as proxy_mod
    from services.health_service import health_state

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "rate limit exceeded"}},
                              headers={"retry-after": "30"})

    c, key = _proxy_test_client(handler, [
        {"name": "p-429", "base_url": "https://only429/v1", "api_key": "k", "models": ["m9"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m9", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                   headers={"Authorization": "Bearer " + key})
    finally:
        c.__exit__(None, None, None)
        proxy_mod.httpx = httpx

    assert r.status_code == 503, r.text
    assert health_state.check_rate_limit("p-429") is True, "429 后该 provider 应进冷却"

