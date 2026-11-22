"""统一鉴权：管理面（本机信任 / 局域网独立密码 + 短期会话）与客户端面（API key）彻底分离。

- 本机（socket 对端为 127.0.0.1 / ::1）访问管理接口：直接放行，无需登录。
- 局域网访问管理接口：只认 admin_token 或由其签发的短期会话 token。
- 客户端调用 /v1/* 仍用 local_api_key，现有客户端零改动。
- 会话 token 为 HMAC 签名的自包含 token，无服务端状态，改密码后旧会话自动失效。

安全说明：
- 默认不信任任何代理，本机判定只看 TCP 对端地址，X-Forwarded-For 被完全忽略。
- 若套了反向代理，需在 config.json 的 trusted_proxies 填入代理 IP/CIDR；
  只有直连对端在该白名单内时才解析代理头，并从右往左跳过可信跳，防伪造。
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from config.settings import settings

security = HTTPBearer(auto_error=False)
SESSION_TTL_SECONDS = 12 * 3600

# 视为"本机"的 socket 对端地址
LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}


def _ip_in_networks(ip: str, networks: list[str]) -> bool:
    """判断 IP 是否落在可信代理列表（支持 CIDR）。无效配置一律不匹配。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in networks:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            continue
    return False


def get_client_ip(request: Request) -> str:
    """解析真实客户端 IP。

    - 直连对端不在 trusted_proxies 时：完全忽略 X-Forwarded-For（防伪造）。
    - 直连对端可信时：从右往左跳过可信代理跳，取最右侧的不可信地址。
    """
    client = request.client
    direct = client.host if client else ""
    if not direct:
        return ""
    trusted = settings.load().trusted_proxies or []
    if not _ip_in_networks(direct, trusted):
        return direct

    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return direct
    chain = [ip.strip() for ip in xff.split(",") if ip.strip()]
    for ip in reversed(chain):
        if not _ip_in_networks(ip, trusted):
            return ip
    return chain[0] if chain else direct


def is_local_request(request: Request) -> bool:
    """判断请求是否来自本机（支持显式配置的可信代理透传）。"""
    return get_client_ip(request).lower() in LOCAL_HOSTS


def _signing_key() -> bytes:
    cfg = settings.load()
    return (cfg.admin_token or cfg.local_api_key).encode("utf-8")


def issue_session_token() -> str:
    """签发一个短期管理会话 token。"""
    payload = {
        "exp": int(time.time()) + SESSION_TTL_SECONDS,
        "nonce": secrets.token_hex(8),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_session_token(token: str) -> bool:
    """校验会话 token 的签名与有效期。"""
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_signing_key(), body.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return int(payload["exp"]) > time.time()
    except Exception:
        return False


def verify_admin_password(password: str) -> bool:
    """校验管理密码。"""
    cfg = settings.load()
    candidate = password or ""
    return bool(candidate) and bool(cfg.admin_token) and hmac.compare_digest(
        candidate, cfg.admin_token
    )


def credential_is_valid(token: str) -> bool:
    """管理凭据是否有效：admin_token 本身，或其签发的短期会话。"""
    if not token:
        return False
    cfg = settings.load()
    if cfg.admin_token and hmac.compare_digest(token, cfg.admin_token):
        return True
    return verify_session_token(token)


async def verify_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """管理接口鉴权依赖：本机直接放行；局域网需有效管理凭据。"""
    if is_local_request(request):
        return "local"
    token = credentials.credentials if credentials else ""
    if credential_is_valid(token):
        return token
    raise HTTPException(status_code=401, detail="Invalid admin credentials")
