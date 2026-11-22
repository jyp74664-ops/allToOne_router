"""鉴权回归测试：本机信任、局域网密码、代理透传、两套密钥隔离。"""
from fastapi.testclient import TestClient
import app
from api.auth import get_client_ip, is_local_request
from config.settings import settings
from starlette.requests import Request


def _scope(host="192.168.1.50", headers=None):
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": headers or [],
        "client": (host, 12345),
        "server": ("test", 80),
        "scheme": "http",
    }


def test_local_request_bypass(client, local_client, admin_headers):
    """本机访问无需 token；局域网无 token 必须 401。"""
    assert local_client.get("/api/providers").status_code == 200
    assert local_client.get("/metrics").status_code == 200
    assert client.get("/api/providers").status_code == 401
    assert client.get("/metrics").status_code == 401


def test_admin_login_and_session(client):
    """正确密码签发会话，错误密码拒绝。"""
    assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 401
    r = client.post("/api/admin/login", json={"password": settings.load().admin_token})
    assert r.status_code == 200
    token = r.json()["token"]
    h = {"Authorization": "Bearer " + token}
    assert client.get("/api/providers", headers=h).status_code == 200
    assert client.get("/api/admin/auth-status", headers=h).json()["need_login"] is False


def test_client_key_cannot_access_admin(client):
    """客户端 key 只能调 /v1/*，不能进管理接口。"""
    key = settings.load().local_api_key
    h = {"Authorization": "Bearer " + key}
    assert client.get("/api/providers", headers=h).status_code == 401
    assert client.get("/v1/models", headers=h).status_code == 200


def test_home_no_plaintext_key(local_client):
    """首页 HTML 不得包含明文客户端 key。"""
    html = local_client.get("/").text
    assert settings.load().local_api_key not in html


def test_auth_status(client, local_client):
    assert local_client.get("/api/admin/auth-status").json() == {"need_login": False, "local": True}
    assert client.get("/api/admin/auth-status").json() == {"need_login": True, "local": False}


def test_forged_xff_ignored_by_default(client, monkeypatch):
    """默认不信任代理，伪造 X-Forwarded-For 不能伪装本机。"""
    assert client.get(
        "/api/admin/auth-status", headers={"X-Forwarded-For": "127.0.0.1"}
    ).json()["need_login"] is True


def test_get_client_ip_trusted_proxy(monkeypatch):
    """可信代理下从右往左跳过可信跳，取真实客户端 IP。"""
    cfg = settings.load()
    monkeypatch.setattr(cfg, "trusted_proxies", ["127.0.0.1"])
    req = Request(_scope("127.0.0.1", [(b"x-forwarded-for", b"1.2.3.4, 127.0.0.1")]))
    assert get_client_ip(req) == "1.2.3.4"
    assert not is_local_request(req)

    req2 = Request(_scope("127.0.0.1", [(b"x-forwarded-for", b"127.0.0.1")]))
    assert is_local_request(req2)


def test_get_client_ip_cidr(monkeypatch):
    """CIDR 白名单同样生效。"""
    cfg = settings.load()
    monkeypatch.setattr(cfg, "trusted_proxies", ["10.0.0.0/8"])
    req = Request(_scope("10.1.2.3", [(b"x-forwarded-for", b"192.168.5.5")]))
    assert get_client_ip(req) == "192.168.5.5"


def test_malformed_trusted_proxy_fails_closed(monkeypatch):
    """无效白名单配置必须 fail-closed，不能放行伪造头。"""
    cfg = settings.load()
    monkeypatch.setattr(cfg, "trusted_proxies", ["not-an-ip"])
    req = Request(_scope("192.168.1.9", [(b"x-forwarded-for", b"127.0.0.1")]))
    assert get_client_ip(req) == "192.168.1.9"
    assert not is_local_request(req)
