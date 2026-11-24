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
        "/api/client-key", "/api/model-details", "/api/lang-hint", "/api/check-concurrency",
        "/api/cost?days=1", "/api/quotas", "/api/request-logs?hours=1",
        "/api/cache-stats", "/api/routing-strategy", "/api/virtual-keys",
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


def test_stability_returns_real_speed_only(initialized_db, local_client):
    """稳定性接口不再按探针 token 估算速度（该字段与已下线的列一起删掉）。"""
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
    assert "tokens_per_sec" not in row
    # 真实测速没跑过时，这几个字段必须是 None，不能拿探针数据凑
    assert row["real_tps"] is None and row["ttft_ms"] is None


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
    # 探针口径的 tokens_per_sec 已随列下线一起删除（审计 #19）：
    # 它用「成功但没返回 usage」的请求做分母，系统性低估，留着只会误导
    assert "tokens_per_sec" not in row


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
    # 进程级连接池会绕开 MockTransport 去连真实网络，测试期强制走「每次新建」路径
    import services.upstream_client as upstream_mod
    upstream_mod.disable_shared_client()

    c = TestClient(app.app, client=("127.0.0.1", 50002))
    real_exit = c.__exit__

    def _exit(*args):
        upstream_mod.enable_shared_client()
        proxy_mod.httpx = httpx
        return real_exit(*args)

    c.__exit__ = _exit
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


# ============================================================
# 审计修复回归（2026-11-23 第二批：404 复测 / 测速任务引用 / 别名 / 检测互斥）
# ============================================================

def test_reconfirm_404_candidate_recovers_on_second_probe():
    """第一次 404 只算候选；复测恢复成 200 就不退役（审计 #8）。"""
    import httpx
    from services.health_service import reconfirm_retire_candidates, health_state

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    class P:
        name = "p-recover"
        base_url = "https://ok/v1"
        api_key = "k"

    key = "p-recover||m"
    results = {key: {"status": "deleted", "code": 404}}
    try:
        confirmed = asyncio.run(reconfirm_retire_candidates(
            _mock_http(handler), {}, {"p-recover": ["m"]}, results,
            {"p-recover": P()}, delay=0,
        ))
        assert confirmed == {}, confirmed
        assert results[key]["status"] == "ok", "复测结果要写回 results"
    finally:
        health_state.health_status.pop(key, None)


def test_reconfirm_404_twice_confirms_retire():
    """两次都 404 才确认退役。"""
    import httpx
    from services.health_service import reconfirm_retire_candidates, health_state

    def handler(request):
        return httpx.Response(404, json={"error": {"message": "model not found"}})

    class P:
        name = "p-gone"
        base_url = "https://gone/v1"
        api_key = "k"

    key = "p-gone||m"
    try:
        confirmed = asyncio.run(reconfirm_retire_candidates(
            _mock_http(handler), {}, {"p-gone": ["m"]}, {},
            {"p-gone": P()}, delay=0,
        ))
        assert confirmed == {"p-gone": ["m"]}, confirmed
    finally:
        health_state.health_status.pop(key, None)


def test_full_check_keeps_model_when_first_404_then_recovers(initialized_db, monkeypatch):
    """全量检测：单次 404 不再直接退役，复测恢复即保留（审计 #8）。"""
    from sqlalchemy import select
    from database import engine as engine_mod
    from database.models import Provider
    from services import health_service

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="p-flaky", base_url="https://flaky/v1", api_key="k",
                           models=["m1"], disabled_models=[], retired_models=[],
                           free_models=[], free_only=False, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    calls = []

    async def fake_check(client, base_url, api_key, model, aliases, max_retries=2):
        calls.append(model)
        if len(calls) == 1:
            return {"status": "deleted", "code": 404, "latency_ms": 20}
        return {"status": "ok", "code": 200, "latency_ms": 20}

    monkeypatch.setattr(health_service, "check_model", fake_check)
    monkeypatch.setattr(health_service, "RETIRE_RECONFIRM_DELAY", 0)

    asyncio.run(health_service.run_full_check())
    assert len(calls) == 2, f"应探测 + 复测各一次，实际 {calls}"

    async def read():
        async with db.SessionLocal() as s:
            return (await s.execute(select(Provider).where(Provider.name == "p-flaky"))).scalar_one()
    p = asyncio.run(read())
    assert p.models == ["m1"], "复测恢复的模型不能被退役删除"
    assert list(p.retired_models or []) == []


def test_full_check_retires_only_after_second_404(initialized_db, monkeypatch):
    """连续两次 404 才真正退役：从 models 摘掉并写进 retired_models。"""
    from sqlalchemy import select
    from database import engine as engine_mod
    from database.models import Provider
    from services import health_service

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="p-dead", base_url="https://dead/v1", api_key="k",
                           models=["gone-model"], disabled_models=[], retired_models=[],
                           free_models=[], free_only=False, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    async def fake_check(client, base_url, api_key, model, aliases, max_retries=2):
        return {"status": "deleted", "code": 404, "latency_ms": 20}

    monkeypatch.setattr(health_service, "check_model", fake_check)
    monkeypatch.setattr(health_service, "RETIRE_RECONFIRM_DELAY", 0)

    asyncio.run(health_service.run_full_check())

    async def read():
        async with db.SessionLocal() as s:
            return (await s.execute(select(Provider).where(Provider.name == "p-dead"))).scalar_one()
    p = asyncio.run(read())
    assert p.models == [], "两次 404 后应从 models 移除"
    assert "gone-model" in (p.retired_models or [])


def test_speed_create_run_keeps_task_reference(monkeypatch):
    """测速任务必须被强引用持有，否则可能被 GC 回收导致进度卡死（审计 #11）。"""
    from services import speed_service

    async def scenario():
        async def fake_run(run_id, items, max_tokens):
            await asyncio.sleep(0.02)

        monkeypatch.setattr(speed_service, "run_speed_test", fake_run)
        rid = speed_service.create_run([("p", "m")], 128)
        try:
            assert rid in speed_service._RUN_TASKS, "create_run 后必须持有 Task 引用"
            await asyncio.sleep(0.15)
            assert rid not in speed_service._RUN_TASKS, "任务完成后应清理引用"
        finally:
            speed_service._RUNS.pop(rid, None)

    asyncio.run(scenario())


def test_save_routers_rejects_duplicate_alias(initialized_db):
    """两个路由组用同一别名：明确报错，而不是写进一条会让请求 500 的数据（审计 #12）。"""
    import pytest
    from services.router_manager import RouterService

    async def scenario():
        await RouterService().save_all({
            "a": {"models": [], "alias": "shared"},
            "b": {"models": [], "alias": "shared"},
        })

    with pytest.raises(ValueError) as exc:
        asyncio.run(scenario())
    assert "shared" in str(exc.value)


def test_save_routers_rejects_alias_equal_to_other_group_name(initialized_db):
    """别名撞上另一个组的组名同样拒绝。"""
    import pytest
    from services.router_manager import RouterService

    async def scenario():
        await RouterService().save_all({
            "a": {"models": [], "alias": "b"},
            "b": {"models": [], "alias": None},
        })

    with pytest.raises(ValueError):
        asyncio.run(scenario())


def test_normalize_router_name_prefers_group_name(initialized_db):
    """组名与别人的别名相同时：不抛异常，且组名优先（审计 #12）。"""
    from database import engine as engine_mod
    from database.models import RouterGroup
    from api.proxy import normalize_router_name

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            # 历史数据里存在的形状：a 的别名撞上了 b 的组名
            s.add(RouterGroup(name="a", alias="b", models=[]))
            s.add(RouterGroup(name="b", alias=None, models=[]))
            await s.commit()
    asyncio.run(seed())

    assert asyncio.run(normalize_router_name("b")) == "b"
    assert asyncio.run(normalize_router_name("a")) == "a"
    assert asyncio.run(normalize_router_name("not-exist")) == "not-exist"


def test_check_all_returns_409_when_busy(local_client):
    """已有一轮检测在跑时，第二次请求直接 409（审计 #13）。"""
    from api import admin as admin_mod

    token = admin_mod._acquire_check("全量检测")
    assert token
    try:
        r = local_client.post("/api/check/all")
        assert r.status_code == 409, r.text
        assert "进行中" in r.text
    finally:
        admin_mod._release_check(token)


def test_check_all_releases_slot_after_run(local_client, monkeypatch):
    """检测跑完后必须释放检测位，否则后续再也点不动。"""
    from api import admin as admin_mod
    from services import health_service

    async def fake_full_check(on_progress=None):
        return {}

    monkeypatch.setattr(health_service, "run_full_check", fake_full_check)
    try:
        r = local_client.post("/api/check/all")
        assert r.status_code == 200, r.text
        assert admin_mod._CHECK_STATE["running"] is False, "任务结束应释放检测位"
    finally:
        admin_mod._CHECK_STATE.update(running=False, kind="", started=0.0, token="")


def test_acquire_check_takes_over_stale_slot():
    """上一轮异常退出留下的标记超时后可以被接管，不会永久卡住。"""
    import time as _time
    from api import admin as admin_mod

    admin_mod._CHECK_STATE.update(
        running=True, kind="旧任务",
        started=_time.time() - admin_mod.CHECK_STALE_SECONDS - 10, token="old",
    )
    token = ""
    try:
        token = admin_mod._acquire_check("新任务")
        assert token, "过期标记应被强制释放"
        assert admin_mod._CHECK_STATE["kind"] == "新任务"
    finally:
        admin_mod._release_check(token)
        admin_mod._CHECK_STATE.update(running=False, kind="", started=0.0, token="")




# ============================================================
# 审计修复回归（2026-11-23 第三批 / P2：性能与可维护性）
# ============================================================

def test_quality_score_uses_sliding_window():
    """质量分只看最近 QUALITY_WINDOW 次：修好的模型必须能回到 1.0（审计 #16）。"""
    from services.health_service import HealthState, QUALITY_WINDOW

    hs = HealthState()
    key = "p||m"
    # 先连续失败 QUALITY_WINDOW 次：分数应归零
    for _ in range(QUALITY_WINDOW):
        hs.update_quality(key, {"status": "fail"})
    assert hs.get_quality_score(key) == 0.0
    # 再成功 QUALITY_WINDOW 次：窗口把失败全部挤出，分数必须回到 1.0
    # （累计口径下这里只能是 0.5，历史失败永久拉低排序）
    for _ in range(QUALITY_WINDOW):
        hs.update_quality(key, {"status": "ok", "latency_ms": 100})
    assert hs.get_quality_score(key) == 1.0
    assert hs.get_test_count(key) == QUALITY_WINDOW


def test_prefix_provider_lookup_does_not_break_early(initialized_db, monkeypatch):
    """提供商名字互为前缀时，前缀命中但模型不在该家要继续往后找（审计 #17）。

    "p" 与 "p-2"：请求 "p-2-m1" 时先命中 "p-" 前缀，rest="2-m1" 不在 p 的
    模型里。原来的 break 会在这里跳出，导致 "p-2" 永远匹配不到、请求 503。
    """
    import httpx
    from services.health_service import health_state

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hit"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "p", "base_url": "https://p/v1", "api_key": "k1", "models": ["other"]},
        {"name": "p-2", "base_url": "https://p2/v1", "api_key": "k2", "models": ["m1"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "p-2-m1", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer " + key})
    finally:
        c.__exit__(None, None, None)

    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hit"
    assert r.json()["model"].startswith("p-2"), r.json()["model"]


def test_run_full_check_probes_concurrently(initialized_db, monkeypatch):
    """全量检测必须并发执行，不能一个个串着等（审计 #15）。"""
    import asyncio
    from database import engine as engine_mod
    from services import health_service as hs

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(Provider(name="conc", base_url="https://x/v1", api_key="k",
                           models=[f"m{i}" for i in range(6)],
                           disabled_models=[], retired_models=[], free_models=[],
                           free_only=False, check_enabled=True))
            await s.commit()
    asyncio.run(seed())

    state = {"inflight": 0, "peak": 0}

    async def fake_check(client, base_url, api_key, model, aliases, **kw):
        state["inflight"] += 1
        state["peak"] = max(state["peak"], state["inflight"])
        await asyncio.sleep(0.05)
        state["inflight"] -= 1
        return {"status": "ok", "code": 200, "latency_ms": 50}

    monkeypatch.setattr(hs, "check_model", fake_check)

    async def no_retire(*a, **kw):
        return {}
    monkeypatch.setattr(hs, "retire_models", no_retire)

    results = asyncio.run(hs.run_full_check())
    assert len(results) == 6, results
    assert state["peak"] >= 2, f"并发度为 1（peak={state['peak']}），说明还是串行"
    assert state["peak"] <= 6


def test_lang_hint_off_leaves_messages_untouched():
    """关闭语言提示后不能再往 messages 里塞 system（审计 #23）。"""
    from api.proxy import ensure_lang_reply

    body = {"messages": [{"role": "user", "content": "把结果输出成 JSON"}]}
    out = ensure_lang_reply(body, "off")
    assert len(out["messages"]) == 1
    assert out["messages"][0]["role"] == "user"

    # auto 模式下照旧注入
    body2 = {"messages": [{"role": "user", "content": "你好，介绍一下自己"}]}
    out2 = ensure_lang_reply(body2, "auto")
    assert out2["messages"][0]["role"] == "system"


def test_verify_client_rejects_wrong_key(initialized_db):
    """客户端鉴权改 hmac.compare_digest 后行为不变：错 Key 一律 401（审计 #24）。"""
    from fastapi.testclient import TestClient
    import app

    c = TestClient(app.app, client=("127.0.0.1", 50003))
    c.__enter__()
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer sk-wrong"})
        assert r.status_code == 401, r.text
        r2 = c.post("/v1/chat/completions",
                    json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert r2.status_code == 401, r2.text
    finally:
        c.__exit__(None, None, None)


def test_login_throttled_after_repeated_failures(local_client, monkeypatch):
    """管理登录连续失败会被锁定，不能再无限次爆破（审计 #22）。"""
    from api import admin as admin_mod

    monkeypatch.setattr(admin_mod, "LOGIN_MAX_FAILS", 2)
    monkeypatch.setattr(admin_mod, "_LOGIN_FAILS", {})
    monkeypatch.setattr(admin_mod, "_LOGIN_LOCKED", {})
    try:
        assert local_client.post("/api/admin/login", json={"password": "bad1"}).status_code == 401
        assert local_client.post("/api/admin/login", json={"password": "bad2"}).status_code == 401
        r = local_client.post("/api/admin/login", json={"password": "bad3"})
        assert r.status_code == 429, r.text
        assert "Retry-After" in r.headers
    finally:
        admin_mod._LOGIN_FAILS.clear()
        admin_mod._LOGIN_LOCKED.clear()


def test_describe_fetch_error_separates_layers():
    """拉模型列表失败的提示要能区分 DNS / 连接 / 鉴权 / 超时（审计 #20）。"""
    import httpx
    from services.health_service import describe_fetch_error

    dns = describe_fetch_error(httpx.ConnectError(
        "[Errno -2] Name or service not known"))
    assert "DNS" in dns, dns
    assert "Key" not in dns

    refused = describe_fetch_error(httpx.ConnectError("Connection refused"))
    assert "拒绝" in refused, refused

    assert "鉴权" in describe_fetch_error(None, 401)
    assert "限流" in describe_fetch_error(None, 429)
    assert "超时" in describe_fetch_error(httpx.ReadTimeout("timed out"))


def test_dashboard_versions_filter_state_js(local_client):
    """filter-state.js 也要带 ?v=，否则单独改它浏览器还用旧缓存（审计 #21）。"""
    html = local_client.get("/").text
    assert "/static/filter-state.js?v=" in html


# ============================================================
# 2.3 产品能力：成本账本 / 额度管家 / 缓存 / 多协议 / 虚拟 Key
# ============================================================

def test_normalize_price_units():
    """上游两种报价单位都要归一成 USD / 1M token。"""
    from services.cost_service import normalize_price

    assert normalize_price(0) == 0.0
    assert normalize_price(None) is None
    assert normalize_price(-1) is None          # -1 = 动态定价/未知
    assert abs(normalize_price(0.00000015) - 0.15) < 1e-9   # 每 token 报价
    assert normalize_price(0.15) == 0.15                     # 已是每 1M 报价


def test_cost_summary_free_model_savings(initialized_db):
    """免费模型 spent=0，但参照价折算与省下金额要算出来。"""
    from datetime import datetime
    from database.models import UsageRecord, ModelPrice
    from services.cost_service import cost_summary

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(ModelPrice(provider_name="p", model="free-x",
                             prompt_per_mtok=0.0, completion_per_mtok=0.0))
            s.add(UsageRecord(timestamp=datetime.now(), provider="p", model="free-x",
                              upstream_model="free-x", prompt_tokens=1_000_000,
                              completion_tokens=0, total_tokens=1_000_000))
            await s.commit()
    asyncio.run(seed())

    data = asyncio.run(cost_summary(30))
    total = data["total"]
    assert total["spent"] == 0.0
    assert abs(total["reference"] - 0.15) < 1e-9
    assert abs(total["saved"] - total["reference"]) < 1e-9
    assert total["unpriced_tokens"] == 0
    assert data["by_model"][0]["priced"] is True


def test_cost_summary_unpriced_kept_separate(initialized_db):
    """没有报价的模型不能算成 0 花费，要单列 unpriced_tokens。"""
    from datetime import datetime
    from database.models import UsageRecord
    from services.cost_service import cost_summary

    db = engine_mod.db

    async def seed():
        async with db.SessionLocal() as s:
            s.add(UsageRecord(timestamp=datetime.now(), provider="p", model="mystery",
                              upstream_model="mystery", prompt_tokens=500,
                              completion_tokens=500, total_tokens=1000))
            await s.commit()
    asyncio.run(seed())

    total = asyncio.run(cost_summary(30))["total"]
    assert total["spent"] == 0.0
    assert total["unpriced_tokens"] == 1000


def test_quota_exhausted_after_daily_limit(initialized_db):
    """今日请求到顶后 is_exhausted 必须为 True，检测会被跳过。"""
    from datetime import datetime
    from database.models import UsageRecord
    from services.quota_service import is_exhausted, save_quota

    db = engine_mod.db
    asyncio.run(save_quota("p", daily_requests=2))

    async def seed():
        async with db.SessionLocal() as s:
            for _ in range(2):
                s.add(UsageRecord(timestamp=datetime.now(), provider="p", model="m",
                                  prompt_tokens=10, completion_tokens=5, total_tokens=15))
            await s.commit()
    asyncio.run(seed())

    exhausted, why = asyncio.run(is_exhausted("p"))
    assert exhausted is True
    assert "请求" in why

    # 没配置配额的家永远不拦
    assert asyncio.run(is_exhausted("other"))[0] is False


def test_cache_only_deterministic_requests():
    """缓存边界：流式 / 高温 / 带 tools / 未给温度 都不缓存。"""
    from services import cache_service

    assert cache_service.cacheable({"stream": False, "temperature": 0.0})
    assert not cache_service.cacheable({"stream": True, "temperature": 0.0})
    assert not cache_service.cacheable({"stream": False, "temperature": 0.7})
    assert not cache_service.cacheable({"stream": False, "temperature": 0.0,
                                        "tools": [{"type": "function"}]})
    assert not cache_service.cacheable({"stream": False})

    key = cache_service.make_key({"messages": [{"role": "user", "content": "hi"}]}, "m")
    cache_service.clear()
    cache_service.put(key, {"choices": [{"message": {"content": "cached"}}]})
    got = cache_service.get(key, 60)
    assert got["choices"][0]["message"]["content"] == "cached"
    got["choices"][0]["message"]["content"] = "mutated"
    # 返回必须是深拷贝，调用方改了不能污染缓存
    assert cache_service.get(key, 60)["choices"][0]["message"]["content"] == "cached"
    assert cache_service.get(key, 0) is None
    cache_service.clear()


def test_anthropic_translation_roundtrip():
    from api.protocols import _anthropic_to_chat, _chat_to_anthropic

    chat = _anthropic_to_chat({
        "model": "m", "max_tokens": 64, "system": "be brief",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
    })
    assert chat["messages"][0] == {"role": "system", "content": "be brief"}
    assert chat["messages"][1]["content"] == "hi"
    assert chat["tools"][0]["function"]["name"] == "f"
    assert chat["tool_choice"] == "required"
    assert chat["max_tokens"] == 64

    out = _chat_to_anthropic({
        "choices": [{
            "message": {"content": "yo", "tool_calls": [
                {"id": "call_1", "function": {"name": "f", "arguments": "{\"a\": 1}"}}]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }, "m")
    assert out["content"][0] == {"type": "text", "text": "yo"}
    assert out["content"][1]["type"] == "tool_use"
    assert out["content"][1]["input"] == {"a": 1}
    assert out["stop_reason"] == "tool_use"


def test_gemini_translation_roundtrip():
    from api.protocols import _chat_to_gemini, _gemini_to_chat

    chat = _gemini_to_chat({
        "systemInstruction": {"parts": [{"text": "sys"}]},
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 32, "temperature": 0.1},
    }, "gemini-x")
    assert chat["model"] == "gemini-x"
    assert chat["messages"][0]["role"] == "system"
    assert chat["max_tokens"] == 32

    out = _chat_to_gemini({
        "choices": [{"message": {"content": "yo"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }, "gemini-x")
    assert out["candidates"][0]["content"]["parts"][0]["text"] == "yo"
    assert out["usageMetadata"]["totalTokenCount"] == 5


def test_virtual_key_lifecycle(local_client):
    """虚拟 Key 可访问、可撤销；撤销后立即 401。"""
    from services.virtual_key_service import create_key, delete_key, resolve

    k = asyncio.run(create_key("手机", rpm_limit=5, tpm_limit=0))
    r = local_client.get("/v1/models", headers={"Authorization": "Bearer " + k["key"]})
    assert r.status_code == 200, r.text
    assert asyncio.run(resolve(k["key"]))["name"] == "手机"

    asyncio.run(delete_key(k["id"]))
    assert asyncio.run(resolve(k["key"])) is None
    r2 = local_client.get("/v1/models", headers={"Authorization": "Bearer " + k["key"]})
    assert r2.status_code == 401


def test_request_log_records_success_and_missing_candidate(initialized_db):
    """成功与「无候选」都要落 RequestLog——失败以前完全没有记录。"""
    import httpx
    from sqlalchemy import select
    from database.models import RequestLog

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "good", "base_url": "https://good/v1", "api_key": "k", "models": ["m1"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer " + key})
        assert r.status_code == 200, r.text
        r2 = c.post("/v1/chat/completions",
                    json={"model": "not-a-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer " + key})
        assert r2.status_code == 503, r2.text
    finally:
        c.__exit__(None, None, None)

    async def read():
        async with engine_mod.db.SessionLocal() as s:
            return (await s.execute(select(RequestLog))).scalars().all()
    rows = asyncio.run(read())
    assert any(row.ok and row.provider == "good" for row in rows)
    assert any((not row.ok) and "候选" in (row.error or "") for row in rows)


def test_request_cache_hits_do_not_call_upstream(initialized_db):
    """temperature=0 的重复请求应命中缓存，只打一次上游。"""
    import httpx
    from services import cache_service

    cache_service.clear()
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hit"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "p", "base_url": "https://p/v1", "api_key": "k", "models": ["m1"]},
    ])
    try:
        body = {"model": "m1", "temperature": 0,
                "messages": [{"role": "user", "content": "same question"}]}
        r1 = c.post("/v1/chat/completions", json=body,
                    headers={"Authorization": "Bearer " + key})
        r2 = c.post("/v1/chat/completions", json=body,
                    headers={"Authorization": "Bearer " + key})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.json()["choices"][0]["message"]["content"] == "hit"
        assert calls["n"] == 1, f"上游被打了 {calls['n']} 次，缓存没生效"
        assert "_fg_meta" not in r2.json()   # 内部标记不能透给客户端
    finally:
        c.__exit__(None, None, None)
        cache_service.clear()


def test_context_window_filter_skips_small_model(initialized_db, monkeypatch):
    """请求明显超过模型上下文时直接跳过该候选，而不是打过去换 400。"""
    import httpx
    from services.meta_service import ModelMetaService

    monkeypatch.setattr(ModelMetaService, "context_limits",
                        property(lambda self: {"m1": 100}))

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "should not happen"}}]})

    c, key = _proxy_test_client(handler, [
        {"name": "small", "base_url": "https://s/v1", "api_key": "k", "models": ["m1"]},
    ])
    try:
        r = c.post("/v1/chat/completions", json={
            "model": "m1",
            "messages": [{"role": "user", "content": "x" * 2000}],
        }, headers={"Authorization": "Bearer " + key})
        assert r.status_code == 503, r.text
    finally:
        c.__exit__(None, None, None)


def test_anthropic_endpoint_roundtrip(initialized_db):
    """POST /v1/messages 端到端：OpenAI 上游 → Anthropic 响应体。"""
    import httpx

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "p", "base_url": "https://p/v1", "api_key": "k", "models": ["m1"]},
    ])
    try:
        r = c.post("/v1/messages", json={
            "model": "m1", "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        }, headers={"Authorization": "Bearer " + key})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["type"] == "message"
        assert d["content"][0]["text"] == "hello"
        assert d["usage"]["output_tokens"] == 3
    finally:
        c.__exit__(None, None, None)


def test_gemini_endpoint_roundtrip(initialized_db):
    """POST /v1beta/models/{m}:generateContent 端到端。"""
    import httpx

    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "gem"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "g", "base_url": "https://g/v1", "api_key": "k", "models": ["gm1"]},
    ])
    try:
        r = c.post("/v1beta/models/gm1:generateContent", json={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 16},
        }, headers={"Authorization": "Bearer " + key})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["candidates"][0]["content"]["parts"][0]["text"] == "gem"
        assert d["usageMetadata"]["promptTokenCount"] == 1
    finally:
        c.__exit__(None, None, None)


def test_embeddings_passthrough(initialized_db):
    """POST /v1/embeddings 按模型找到渠道直接透传。"""
    import httpx

    def handler(request):
        assert str(request.url).endswith("/embeddings")
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
            "usage": {"prompt_tokens": 4, "total_tokens": 4},
        })

    c, key = _proxy_test_client(handler, [
        {"name": "e", "base_url": "https://e/v1", "api_key": "k", "models": ["text-embedding-3-small"]},
    ])
    try:
        r = c.post("/v1/embeddings", json={
            "model": "text-embedding-3-small", "input": "hi",
        }, headers={"Authorization": "Bearer " + key})
        assert r.status_code == 200, r.text
        assert r.json()["data"][0]["embedding"] == [0.1, 0.2]
    finally:
        c.__exit__(None, None, None)


def test_upstream_error_body_survives_to_client_and_log(initialized_db):
    """上游 4xx/5xx 的原文必须留到响应 detail 与 request_logs.error。

    以前 proxy 只把状态码写进 last_error，400 到底是「内容超长」「参数非法」还是
    「额度不足」全丢了，客户端只能看到一句 503。
    这里用 Google 原生格式（顶层是数组）覆盖「顶层不是对象」那条分支。
    """
    import httpx
    from sqlalchemy import select
    from database import engine as engine_mod
    from database.models import RequestLog

    def handler(request):
        return httpx.Response(400, json=[{
            "error": {"code": 400, "message": "The input token count (900000) exceeds the maximum allowed"}
        }])

    c, key = _proxy_test_client(handler, [
        {"name": "p-errbody", "base_url": "https://errbody/v1", "api_key": "k", "models": ["m-errbody"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m-errbody", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer " + key})
    finally:
        c.__exit__(None, None, None)

    assert r.status_code == 503, r.text
    assert "exceeds the maximum allowed" in r.text, r.text

    async def read_logs():
        async with engine_mod.db.SessionLocal() as s:
            return (await s.execute(select(RequestLog))).scalars().all()

    logs = asyncio.run(read_logs())
    assert logs, "失败请求也必须落 request_logs"
    assert "exceeds the maximum allowed" in (logs[-1].error or ""), logs[-1].error


def test_no_candidate_reason_is_not_always_token_count(initialized_db):
    """无候选时的文案要说明真实原因（熔断/上轮失败），不能一律甩锅给 token 数。"""
    import httpx
    from services.health_service import health_state

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "never"}}]})

    health_state.health_status["p-unhealthy||m-unhealthy"] = {"status": "fail"}
    try:
        c, key = _proxy_test_client(handler, [
            {"name": "p-unhealthy", "base_url": "https://uh/v1", "api_key": "k", "models": ["m-unhealthy"]},
        ])
        try:
            r = c.post("/v1/chat/completions",
                       json={"model": "m-unhealthy", "messages": [{"role": "user", "content": "hi"}]},
                       headers={"Authorization": "Bearer " + key})
        finally:
            c.__exit__(None, None, None)
    finally:
        health_state.health_status.pop("p-unhealthy||m-unhealthy", None)

    assert r.status_code == 503, r.text
    assert "熔断" in r.text, r.text
    assert "超出全部" not in r.text, r.text


def test_upstream_error_body_survives_to_client_and_log(initialized_db):
    """上游 4xx/5xx 的原文必须留到响应 detail 与 request_logs.error。

    以前 proxy 只把状态码写进 last_error，400 到底是「内容超长」「参数非法」还是
    「额度不足」全丢了，客户端只能看到一句 503。
    这里用 Google 原生格式（顶层是数组）覆盖「顶层不是对象」那条分支。
    """
    import httpx
    from sqlalchemy import select
    from database import engine as engine_mod
    from database.models import RequestLog

    def handler(request):
        return httpx.Response(400, json=[{
            "error": {"code": 400, "message": "The input token count (900000) exceeds the maximum allowed"}
        }])

    c, key = _proxy_test_client(handler, [
        {"name": "p-errbody", "base_url": "https://errbody/v1", "api_key": "k", "models": ["m-errbody"]},
    ])
    try:
        r = c.post("/v1/chat/completions",
                   json={"model": "m-errbody", "messages": [{"role": "user", "content": "hi"}]},
                   headers={"Authorization": "Bearer " + key})
    finally:
        c.__exit__(None, None, None)

    assert r.status_code == 503, r.text
    assert "exceeds the maximum allowed" in r.text, r.text

    async def read_logs():
        async with engine_mod.db.SessionLocal() as s:
            return (await s.execute(select(RequestLog))).scalars().all()

    logs = asyncio.run(read_logs())
    assert logs, "失败请求也必须落 request_logs"
    assert "exceeds the maximum allowed" in (logs[-1].error or ""), logs[-1].error


def test_no_candidate_reason_is_not_always_token_count(initialized_db):
    """无候选时的文案要说明真实原因（熔断/上轮失败），不能一律甩锅给 token 数。"""
    import httpx
    from services.health_service import health_state

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "never"}}]})

    health_state.health_status["p-unhealthy||m-unhealthy"] = {"status": "fail"}
    try:
        c, key = _proxy_test_client(handler, [
            {"name": "p-unhealthy", "base_url": "https://uh/v1", "api_key": "k", "models": ["m-unhealthy"]},
        ])
        try:
            r = c.post("/v1/chat/completions",
                       json={"model": "m-unhealthy", "messages": [{"role": "user", "content": "hi"}]},
                       headers={"Authorization": "Bearer " + key})
        finally:
            c.__exit__(None, None, None)
    finally:
        health_state.health_status.pop("p-unhealthy||m-unhealthy", None)

    assert r.status_code == 503, r.text
    assert "熔断" in r.text, r.text
    assert "超出全部" not in r.text, r.text


def test_request_logs_filter_by_route(local_client, initialized_db):
    """「最近请求」按路由过滤：model 参数精确匹配 requested_model。

    请求里填的模型名就是路由组名，所以「只看某条路由」等价于按
    requested_model 精确过滤；doTest 这类测试路由不该混进真实调用里。
    """
    from datetime import datetime
    from database import engine as engine_mod
    from database.models import RequestLog

    async def seed():
        async with engine_mod.db.SessionLocal() as s:
            s.add(RequestLog(trace_id="t-real", timestamp=datetime.now(), requested_model="编码",
                             ok=False, status_code=503, latency_ms=10, attempts=0))
            s.add(RequestLog(trace_id="t-test", timestamp=datetime.now(), requested_model="doTest",
                             ok=True, status_code=200, latency_ms=20, attempts=1))
            await s.commit()
    asyncio.run(seed())

    all_rows = local_client.get("/api/request-logs?hours=24").json()["items"]
    assert {r["requested_model"] for r in all_rows} == {"编码", "doTest"}

    only = local_client.get("/api/request-logs?hours=24&model=编码").json()
    assert [r["trace_id"] for r in only["items"]] == ["t-real"]
    assert only["items"][0]["requested_model"] == "编码"
    # 被过滤掉的其他路由也要报条数：页面停在单条路由时，新请求进别的路由
    # （比如「立即检测」写的 doTest）会让用户以为自动刷新没生效
    assert only["other_total"] == 1

    # 不存在的路由名应返回空，而不是退回全量
    none = local_client.get("/api/request-logs?hours=24&model=不存在").json()
    assert none["items"] == []
    assert none["other_total"] == 2

    # 不筛选时不带这个语义（保持 0）
    assert local_client.get("/api/request-logs?hours=24").json()["other_total"] == 0


def test_last_router_roundtrip(local_client, monkeypatch):
    """最后一次保存的路由组名要能读能写，供「最近请求」和路由弹窗做默认值。"""
    from config.settings import settings as settings_obj

    # 不落盘：这个接口真实实现会写 config.json，测试里只验证内存往返
    monkeypatch.setattr(settings_obj, "save", lambda cfg: None)
    monkeypatch.setattr(settings_obj.load(), "last_router", "")

    assert local_client.get("/api/last-router").json()["name"] == ""

    r = local_client.post("/api/last-router", json={"name": "中文长文本"})
    assert r.status_code == 200, r.text
    assert local_client.get("/api/last-router").json()["name"] == "中文长文本"

    # 清空（删除路由组时前端会这么调）
    local_client.post("/api/last-router", json={"name": ""})
    assert local_client.get("/api/last-router").json()["name"] == ""

    assert local_client.post("/api/last-router", json={"name": "x" * 200}).status_code == 400


def test_request_log_survives_client_cancel(initialized_db):
    """客户端中途断开时请求日志不能丢。

    病灶：取消状态下任何 await 都会立刻再抛 CancelledError，以前流式
    finally 里直接 `await record_request(...)` 根本跑不完，表现成
    「明明调用了接口，最近请求里什么都没有」。
    """
    from sqlalchemy import select
    from api import proxy as proxy_mod
    from database.models import RequestLog

    db = initialized_db

    async def scenario():
        async def worker():
            asyncio.current_task().cancel()  # 模拟请求被客户端中断
            await proxy_mod._record_safely(
                trace_id="cancel-trace", requested_model="取消中的请求",
                stream=True, ok=False, error="流未正常结束（客户端断开或上游截断）",
            )

        task = asyncio.ensure_future(worker())
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 等后台写库任务收尾
        for _ in range(300):
            if not proxy_mod._bg_tasks:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())

    async def read_rows():
        async with db.SessionLocal() as s:
            res = await s.execute(select(RequestLog).where(RequestLog.trace_id == "cancel-trace"))
            return res.scalars().all()

    rows = asyncio.run(read_rows())
    assert len(rows) == 1, "被取消的请求也必须留下一条日志"
    assert rows[0].ok is False
    assert rows[0].stream is True
    assert "客户端断开" in (rows[0].error or "")


def test_middleware_backfills_when_forward_never_logs(initialized_db, local_client, monkeypatch):
    """转发流程一条都没记时，中间件要补一条。

    病灶：流式响应已经 return，客户端在 body 迭代前断开 → async generator
    从未启动 → 它的 finally 不执行 → 记账代码一行都跑不到。此时接口返回 200、
    最近请求里却什么都没有。中间件在请求结束后对账补记。
    """
    from sqlalchemy import select
    from fastapi.responses import JSONResponse
    from api import proxy as proxy_mod
    from database.models import RequestLog

    seen: list[str] = []

    async def fake_forward(*args, **kwargs):
        seen.append(kwargs.get("trace_id"))
        return JSONResponse({"ok": True})

    monkeypatch.setattr(proxy_mod, "forward_chat", fake_forward)

    from config.settings import settings
    key = settings.load().local_api_key
    r = local_client.post(
        "/v1/chat/completions",
        json={"model": "随便一个路由", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer " + key},
    )
    assert r.status_code == 200
    assert seen and seen[0], "proxy_chat 必须把 trace_id 传给 forward_chat"

    async def read_rows():
        async with initialized_db.SessionLocal() as s:
            res = await s.execute(select(RequestLog).where(RequestLog.trace_id == seen[0]))
            return res.scalars().all()

    rows = asyncio.run(read_rows())
    assert len(rows) == 1, "没记账的请求也必须由中间件补一条"
    assert "兜底补记" in (rows[0].error or "")
    assert rows[0].ok is False


def test_business_rejection_is_not_backfilled(initialized_db, local_client):
    """转发前的业务拒绝（缺 model）按设计不入表，中间件不能补记。"""
    from sqlalchemy import func, select
    from database.models import RequestLog

    async def count_rows():
        async with initialized_db.SessionLocal() as s:
            res = await s.execute(select(func.count()).select_from(RequestLog))
            return res.scalar_one()

    before = asyncio.run(count_rows())
    from config.settings import settings
    key = settings.load().local_api_key
    r = local_client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer " + key},
    )
    assert r.status_code == 400
    assert asyncio.run(count_rows()) == before


def test_stream_logging_runs_before_usage_when_cancelled(initialized_db, monkeypatch):
    """流式 finally 里，记账必须排在 append_usage 之前。

    病灶：append_usage 的 await 在前，且 except Exception 抓不住
    CancelledError（3.8 起它继承 BaseException）。客户端一断开，取消从
    usage 那里穿透出去，finally 直接中断，记账一行都不跑——09:54-09:59
    那批「25 条进站 200、只有 2 条入表」就是这个原因。
    """
    import asyncio
    import httpx
    from api import proxy as proxy_mod

    order: list[str] = []

    async def spy_record(**kwargs):
        order.append("record")

    async def spy_usage(**kwargs):
        order.append("usage")
        raise asyncio.CancelledError()  # 模拟取消态下 usage 写库被打断

    monkeypatch.setattr(proxy_mod, "_record_safely", spy_record)
    monkeypatch.setattr(proxy_mod, "append_usage", spy_usage)

    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    c, key = _proxy_test_client(handler, [
        {"name": "p-sse", "base_url": "https://sse/v1", "api_key": "k", "models": ["m1"]},
    ])
    try:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "m1", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": "Bearer " + key},
        )
    finally:
        c.__exit__(None, None, None)

    assert r.status_code == 200
    assert order[:2] == ["record", "usage"], f"记账必须排在用量之前，实际顺序 {order}"


def test_pre_forward_exception_is_logged(local_client, initialized_db, monkeypatch):
    """转发前的处理抛异常（compress_hermes / ensure_lang_reply 这类）也要留痕。

    病灶：这些函数在 forward_chat 之前执行，一旦抛非 HTTPException，
    FastAPI 直接 500，而记录点全在 forward_chat 内部 —— 结果就是
    「客户端说调过了、最近请求里一条都没有」，且现场完全丢失。
    """
    import asyncio
    from sqlalchemy import select
    from api import proxy as proxy_mod
    from config.settings import settings
    from database.models import RequestLog

    def boom(_body):
        raise ValueError("boom-in-preprocess")

    monkeypatch.setattr(proxy_mod, "compress_hermes", boom)
    key = settings.load().local_api_key

    r = local_client.post(
        "/v1/chat/completions",
        json={"model": "某个路由", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer " + key},
    )
    assert r.status_code == 500

    async def read_rows():
        async with initialized_db.SessionLocal() as s:
            res = await s.execute(
                select(RequestLog).where(RequestLog.status_code == 500)
            )
            return res.scalars().all()

    rows = asyncio.run(read_rows())
    assert len(rows) == 1, "转发前的异常也必须留一条日志"
    assert rows[0].ok is False
    assert rows[0].requested_model == "某个路由"
    assert "网关内部异常" in (rows[0].error or "")

