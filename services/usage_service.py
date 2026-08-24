"""服务层：用量统计"""
import time
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from sqlalchemy import select
from database.models import UsageRecord, HealthRecord
from database.engine import db

logger = logging.getLogger("flowgate.usage")

MAX_HISTORY_DAYS = 30
MAX_USAGE_DAYS = 30
HISTORY_CLEANUP_INTERVAL = 6 * 3600
last_history_cleanup: float = 0


async def append_usage(
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
) -> None:
    """追加一条用量记录"""
    async with db.SessionLocal() as session:
        record = UsageRecord(
            timestamp=datetime.now(),
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        session.add(record)
        await session.commit()


async def read_usage(days: int = 1) -> list[dict]:
    """读取最近 N 天的用量记录"""
    days = max(1, min(days, MAX_USAGE_DAYS))
    async with db.SessionLocal() as session:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(days=days)
        # 使用 ORM 查询而非表对象查询，确保类型正确映射
        stmt = (
            select(UsageRecord)
            .where(UsageRecord.timestamp >= cutoff)
            .order_by(UsageRecord.timestamp.desc())
        )
        result = await session.execute(stmt)
        records = result.scalars().all()
        return [
            {
                "ts": int(r.timestamp.timestamp()) if hasattr(r.timestamp, 'timestamp') else r.timestamp,
                "provider": r.provider,
                "model": r.model,
                "pt": r.prompt_tokens,
                "ct": r.completion_tokens,
                "tt": r.total_tokens,
            }
            for r in records
        ]


async def append_health_history(snapshot: dict) -> None:
    """追加一条健康探测历史"""
    async with db.SessionLocal() as session:
        for key, info in snapshot.items():
            provider_name, model = key.split("||", 1)
            record = HealthRecord(
                provider_name=provider_name,
                model=model,
                status=info.get("status", "unknown"),
                code=info.get("code"),
                latency_ms=info.get("latency_ms"),
                prompt_tokens=info.get("prompt_tokens", 0),
                completion_tokens=info.get("completion_tokens", 0),
                checked_at=datetime.now(),
            )
            session.add(record)
        await session.commit()


async def read_health_history(hours: int = 24) -> list[dict]:
    """读取最近 N 小时的健康历史"""
    async with db.SessionLocal() as session:
        from datetime import timedelta
        cutoff = datetime.now() - timedelta(hours=hours)
        # SQLite 存储为 TEXT，需要用字符串比较
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S.%f')
        stmt = (
            select(HealthRecord)
            .where(HealthRecord.checked_at >= cutoff_str)
            .order_by(HealthRecord.checked_at.desc())
        )
        result = await session.execute(stmt)
        records = result.scalars().all()
        return [
            {
                "time": int(r.checked_at.timestamp()) if hasattr(r.checked_at, 'timestamp') else r.checked_at,
                "data": {
                    r.key: {
                        "status": r.status,
                        "code": r.code,
                        "latency_ms": r.latency_ms,
                        "prompt_tokens": r.prompt_tokens,
                        "completion_tokens": r.completion_tokens,
                    }
                },
            }
            for r in records
        ]


async def cleanup_old_records() -> tuple[int, int]:
    """清理过期记录，返回 (清理的历史数, 清理的用量数)"""
    global last_history_cleanup
    now = time.time()
    if now - last_history_cleanup < HISTORY_CLEANUP_INTERVAL:
        return 0, 0
    
    last_history_cleanup = now
    removed_history = 0
    removed_usage = 0
    
    async with db.SessionLocal() as session:
        from datetime import timedelta
        
        # 清理健康历史
        cutoff_history = datetime.now() - timedelta(days=MAX_HISTORY_DAYS)
        result = await session.execute(
            HealthRecord.__table__.delete().where(HealthRecord.checked_at < cutoff_history)
        )
        removed_history = result.rowcount
        
        # 清理用量记录
        cutoff_usage = datetime.now() - timedelta(days=MAX_USAGE_DAYS)
        result = await session.execute(
            UsageRecord.__table__.delete().where(UsageRecord.timestamp < cutoff_usage)
        )
        removed_usage = result.rowcount
        
        await session.commit()
    
    return removed_history, removed_usage


from datetime import datetime
