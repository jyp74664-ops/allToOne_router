"""服务层：虚拟 Key（多 Key + RPM/TPM 限流 + 用量归属）。

主 Key（config.local_api_key）始终有效且不限流；虚拟 Key 给手机、同事、
临时脚本用，可以单独撤销、单独限速。限流窗口在进程内存里，重启清零——
对自用网关足够，也不值得为它引入 Redis。
"""
import logging
import secrets
import threading
import time
from collections import deque
from datetime import datetime

from sqlalchemy import select

from database.engine import db
from database.models import VirtualKey

logger = logging.getLogger("flowgate.vkey")

# key_id -> deque[(timestamp, tokens)]
_windows: dict[int, deque] = {}
_lock = threading.Lock()
WINDOW_SECONDS = 60


async def list_keys() -> list[dict]:
    async with db.SessionLocal() as session:
        rows = (await session.execute(
            select(VirtualKey).order_by(VirtualKey.created_at.desc())
        )).scalars().all()
    out = []
    for r in rows:
        req_60s, tok_60s = _window_usage(r.id)
        out.append({
            "id": r.id,
            "key": r.key,
            "name": r.name,
            "rpm_limit": r.rpm_limit,
            "tpm_limit": r.tpm_limit,
            "enabled": bool(r.enabled),
            "created_at": r.created_at.isoformat(),
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            "requests_60s": req_60s,
            "tokens_60s": tok_60s,
        })
    return out


async def create_key(name: str, rpm_limit: int = 0, tpm_limit: int = 0) -> dict:
    name = (name or "").strip() or "未命名"
    key = "sk-vk-" + secrets.token_hex(16)
    async with db.SessionLocal() as session:
        row = VirtualKey(
            key=key,
            name=name[:100],
            rpm_limit=max(0, int(rpm_limit or 0)),
            tpm_limit=max(0, int(tpm_limit or 0)),
            enabled=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return {
            "id": row.id, "key": row.key, "name": row.name,
            "rpm_limit": row.rpm_limit, "tpm_limit": row.tpm_limit,
        }


async def delete_key(key_id: int) -> bool:
    async with db.SessionLocal() as session:
        row = await session.get(VirtualKey, key_id)
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
    with _lock:
        _windows.pop(key_id, None)
    return True


async def set_enabled(key_id: int, enabled: bool) -> bool:
    async with db.SessionLocal() as session:
        row = await session.get(VirtualKey, key_id)
        if row is None:
            return False
        row.enabled = bool(enabled)
        await session.commit()
    return True


async def resolve(token: str) -> dict | None:
    """按 token 找启用的虚拟 Key。找不到返回 None（交给主 Key 判断）。"""
    if not token:
        return None
    # 数据库还没初始化（单元测试直接打接口）：当作没有虚拟 Key，
    # 让鉴权走 401，而不是在这里抛 NoneType is not callable
    if db.SessionLocal is None:
        return None
    async with db.SessionLocal() as session:
        row = (await session.execute(
            select(VirtualKey).where(VirtualKey.key == token)
        )).scalar_one_or_none()
        if row is None or not row.enabled:
            return None
        return {
            "id": row.id, "name": row.name,
            "rpm_limit": row.rpm_limit, "tpm_limit": row.tpm_limit,
        }


def _window_usage(key_id: int) -> tuple[int, int]:
    now = time.time()
    with _lock:
        dq = _windows.get(key_id)
        if not dq:
            return 0, 0
        while dq and now - dq[0][0] > WINDOW_SECONDS:
            dq.popleft()
        req = len(dq)
        tok = sum(t for _, t in dq)
    return req, tok


def check_limits(key_id: int, rpm_limit: int, tpm_limit: int) -> tuple[bool, str]:
    """请求前判断是否超限。返回 (允许, 原因)。"""
    req, tok = _window_usage(key_id)
    if rpm_limit and req >= rpm_limit:
        return False, f"该 Key 超过每分钟 {rpm_limit} 次请求限制"
    if tpm_limit and tok >= tpm_limit:
        return False, f"该 Key 超过每分钟 {tpm_limit} token 限制"
    return True, ""


def note_request(key_id: int, tokens: int = 0) -> None:
    now = time.time()
    with _lock:
        dq = _windows.setdefault(key_id, deque())
        dq.append((now, max(0, int(tokens or 0))))
        while dq and now - dq[0][0] > WINDOW_SECONDS:
            dq.popleft()


def add_tokens(key_id: int, tokens: int) -> None:
    """请求成功后把实际 token 补到窗口里最近一条记录上。

    不能用 note_request 再记一次：那会把请求数也加一遍，RPM 直接翻倍。
    """
    if not tokens:
        return
    with _lock:
        dq = _windows.get(key_id)
        if not dq:
            return
        ts, tok = dq[-1]
        dq[-1] = (ts, tok + int(tokens))


def prune() -> None:
    """清掉过期窗口，避免被撤销的 Key 留下内存垃圾。"""
    now = time.time()
    with _lock:
        for key_id in list(_windows):
            dq = _windows[key_id]
            while dq and now - dq[0][0] > WINDOW_SECONDS:
                dq.popleft()
            if not dq:
                _windows.pop(key_id, None)
