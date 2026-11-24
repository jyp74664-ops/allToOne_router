"""可观测性：每个转发请求一条记录（成功与失败都记）。

以前只有 usage_records，且只记成功：上游 402/429/超时时请求直接失败，
库里没有任何现场——不知道打到哪家、第几个候选、原文错误是什么。
这张表就是补这个洞，字段刻意保持扁平，方便直接 SELECT 排查。
"""
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from database.engine import db
from database.models import RequestLog

logger = logging.getLogger("flowgate.obs")

# ---------------------------------------------------------------------------
# 记账对账：判断「这条请求有没有走过记账流程」
#
# 病灶：流式请求的记账写在 async generator 的 finally 里。万一生成器从未被
# 迭代（客户端在响应头发出前后就断开、ASGI 直接结束），finally 根本不执行，
# 记录点一个都不会跑——接口明明返回 200，最近请求里却什么都没有。
# 这里在「有人尝试记账」时打一个标记，中间件在请求彻底结束后对账：标记不在
# 就说明转发流程压根没走到记账，由中间件补一条，保证不再有黑洞。
# ---------------------------------------------------------------------------
_ATTEMPTED: set[str] = set()
_ATTEMPTED_LOCK = threading.Lock()
_ATTEMPTED_MAX = 4000

_RECORD_ERR_LOG = Path(__file__).resolve().parent.parent / "logs" / "record-errors.log"


def mark_attempted(trace_id: str | None) -> None:
    """标记这条 trace 已经走过记账流程（无论最终写库成功与否）。"""
    if not trace_id:
        return
    with _ATTEMPTED_LOCK:
        _ATTEMPTED.add(trace_id)
        if len(_ATTEMPTED) > _ATTEMPTED_MAX:
            for key in list(_ATTEMPTED)[: _ATTEMPTED_MAX // 2]:
                _ATTEMPTED.discard(key)


def was_attempted(trace_id: str | None) -> bool:
    """没有 trace_id 时按「已处理」算，避免中间件瞎补。"""
    if not trace_id:
        return True
    with _ATTEMPTED_LOCK:
        return trace_id in _ATTEMPTED


def _log_write_failure(trace_id: str, exc: BaseException) -> None:
    """写库失败要留痕。以前只有 logger.warning，而 uvicorn 日志只在控制台窗口里，
    重启就没了——这正是「明明有请求、表里却没有」查不出原因的原因。"""
    try:
        _RECORD_ERR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _RECORD_ERR_LOG.exists() and _RECORD_ERR_LOG.stat().st_size > 1024 * 1024:
            _RECORD_ERR_LOG.replace(_RECORD_ERR_LOG.with_name("record-errors.log.1"))
        with _RECORD_ERR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{datetime.now():%m-%d %H:%M:%S} trace={trace_id} "
                f"{type(exc).__name__}: {exc}\n"
            )
    except Exception:  # 留痕失败本身不能影响转发
        pass


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
        _log_write_failure(trace_id, exc)
