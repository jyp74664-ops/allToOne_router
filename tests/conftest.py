"""测试公共夹具：所有测试使用临时数据库与隔离配置，绝不触碰真实 flowgate.db。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """每个测试使用独立的临时 SQLite 数据库。"""
    import database.engine as engine_mod

    fresh = engine_mod.Database(tmp_path)
    monkeypatch.setattr(engine_mod, "db", fresh)

    # 各模块通过 from database.engine import db 持有引用，需逐一替换
    for mod_name in (
        "api.admin", "api.webhooks", "api.metrics", "api.proxy",
        "services.provider_service", "services.router_manager",
        "services.usage_service", "services.meta_service",
        "services.cost_service", "services.quota_service",
        "services.virtual_key_service", "services.observability",
    ):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "db"):
            monkeypatch.setattr(mod, "db", fresh, raising=False)

    yield fresh


@pytest.fixture
def initialized_db(isolated_db):
    """初始化临时库的引擎与表结构（供不经过 TestClient 的测试使用）。"""
    import asyncio
    asyncio.run(isolated_db.init_db())
    return isolated_db


@pytest.fixture
def client(isolated_db, monkeypatch):
    """返回已初始化数据库的 TestClient，来源设为局域网以覆盖鉴权路径。"""
    import asyncio
    from fastapi.testclient import TestClient
    import app
    from config.settings import settings

    cfg = settings.load()
    monkeypatch.setattr(cfg, "trusted_proxies", [])

    asyncio.run(isolated_db.init_db())
    with TestClient(app.app, client=("192.168.1.50", 50000)) as c:
        yield c


@pytest.fixture
def local_client(isolated_db, monkeypatch):
    """来源为本机的 TestClient（免登录）。"""
    from fastapi.testclient import TestClient
    import app
    from config.settings import settings

    cfg = settings.load()
    monkeypatch.setattr(cfg, "trusted_proxies", [])

    with TestClient(app.app, client=("127.0.0.1", 50001)) as c:
        yield c


@pytest.fixture
def admin_headers(client):
    """局域网客户端登录后拿到管理会话 token。"""
    import app
    from config.settings import settings
    pwd = settings.load().admin_token
    # 用同一 client 登录（client fixture 已是局域网来源）
    r = client.post("/api/admin/login", json={"password": pwd})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}
