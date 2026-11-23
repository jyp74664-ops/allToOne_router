"""可观测性：每个转发请求一条记录（成功与失败都记）。

以前只有 usage_records，且只记成功：上游 402/429/超时时请求直接失败，
库里没有任何现场——不知道打到哪家、第几个候选、原文错误是什么。
这张表就是补这个洞，字段刻意保持扁平，方便直接 SELECT 排查。
"""
import logging
import uuid
from datetime import datetime

from database.engine import db
from database.models import RequestLog

logger = logging.getLogger("flowgate.obs")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


async def record_request(
    trace_id: str,
    requested_model: str,
    endpoint: str = "/v1/chat/completions",
    provider: str | None = None,
    upstream_model: str | None = None,
    stream: bool = False,
    ok: bool = False,
    status_code: int | None = None,
    attempts: int = 0,
    latency_ms: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached: bool = False,
    api_key_name: str = "",
    error: str | None = None,
) -> None:
    try:
        async with db.SessionLocal() as session:
            session.add(RequestLog(
                trace_id=trace_id,
                timestamp=datetime.now(),
                endpoint=endpoint,
                requested_model=requested_model,
                provider=provider,
                upstream_model=upstream_model,
                stream=bool(stream),
                ok=bool(ok),
                status_code=status_code,
                attempts=int(attempts or 0),
                latency_ms=latency_ms,
                prompt_tokens=int(prompt_tokens or 0),
                completion_tokens=int(completion_tokens or 0),
                cached=bool(cached),
                api_key_name=api_key_name or "",
                error=(error or "")[:1000] or None,
            ))
            await session.commit()
    except Exception as exc:  # 记日志失败绝不能影响转发
        logger.warning("record_request failed: %s", exc)
