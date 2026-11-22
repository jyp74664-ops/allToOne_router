"""服务层：真实生成速度测速（流式）。

与健康巡检（services.health_service.check_model）的区别：

- 巡检发的是 ``max_tokens=5`` 的**非流式**请求，目的是判断「通不通」。它的耗时里
  绝大部分是网络往返、上游排队和首 token 延迟，除以 token 数得到的所谓「tok/s」
  与 decode 速度无关，只能当相对信号看。
- 测速发 ``max_tokens=128`` 的**流式**请求，记录首 token 延迟（TTFT）和首 token
  之后的解码速率 —— 这才是「每秒生成多少 token」的本义。

只在用户手动点「⚡ 测速」时运行，不参与 30 分钟自动巡检：同样一个模型，
巡检消耗 5 token，测速消耗 128 token，是 25 倍。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import select

from config.settings import settings

logger = logging.getLogger("flowgate.speed")

# 提示词要求「连续散文」，避免模型输出列表/代码导致 token 结构异常
SPEED_PROMPT = (
    "请用一段连贯的说明文字介绍人工智能的发展历程，大约二百字，"
    "不要使用列表、标题或代码块。"
)

DEFAULT_MAX_TOKENS = 128
MAX_ITEMS = 50          # 单次测速的模型上限，防止误点全表烧额度
CONCURRENCY = 3         # 并发数：decode 阶段占带宽很少，适度并发能显著缩短总时长
SPEED_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
KEEP_RUNS = 3           # 内存里只保留最近几次任务，避免无限增长

# run_id -> {started_at, finished, total, done, results[], skipped, max_tokens}
_RUNS: dict[str, dict] = {}


def _delta_text(delta: dict) -> str:
    """取出一个 chunk 的增量文本（兼容 reasoning 模型）。"""
    if not isinstance(delta, dict):
        return ""
    return delta.get("content") or delta.get("reasoning_content") or ""


async def _stream_once(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    actual_model: str,
    max_tokens: int,
    include_usage: bool,
) -> dict:
    """发一次流式请求并计时。返回结果 dict（status=ok / error / retry_no_usage_opt）。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": actual_model,
        "messages": [{"role": "user", "content": SPEED_PROMPT}],
        "max_tokens": max_tokens,
        "stream": True,
    }
    if include_usage:
        # 请求上游在最后一个 chunk 带上 usage，这样 token 数是精确值而不是估算
        payload["stream_options"] = {"include_usage": True}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    t0 = time.perf_counter()
    first_at: Optional[float] = None
    last_at: Optional[float] = None
    chunks = 0
    usage: Optional[dict] = None

    try:
        async with client.stream(
            "POST", url, json=payload, headers=headers, timeout=SPEED_TIMEOUT
        ) as resp:
            if resp.status_code != 200:
                raw = await resp.aread()
                body = raw[:300].decode("utf-8", "ignore")
                # 部分上游不认 stream_options，去掉重试一次（只重试一次，见调用方）
                if include_usage and resp.status_code == 400 and "stream_options" in body:
                    return {"status": "retry_no_usage_opt"}
                return {
                    "status": "error",
                    "code": resp.status_code,
                    "error": body.strip() or f"HTTP {resp.status_code}",
                    "wall_ms": round((time.perf_counter() - t0) * 1000),
                }

            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue

                if obj.get("usage"):
                    usage = obj["usage"]

                for choice in obj.get("choices") or []:
                    piece = _delta_text(choice.get("delta") or {})
                    if not piece:
                        continue
                    now = time.perf_counter()
                    if first_at is None:
                        first_at = now
                    last_at = now
                    chunks += 1
    except Exception as e:  # 超时 / 连接错误 / 流中断
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {e}"[:300],
            "wall_ms": round((time.perf_counter() - t0) * 1000),
        }

    t_end = time.perf_counter()
    wall_ms = round((t_end - t0) * 1000)

    if first_at is None:
        return {
            "status": "error",
            "error": "上游返回 200，但流里没有任何内容增量（可能是空响应或协议不兼容）",
            "wall_ms": wall_ms,
        }

    ttft_ms = round((first_at - t0) * 1000)
    decode_ms = max(0, round(((last_at or first_at) - first_at) * 1000))

    # token 数：优先用上游返回的 usage，拿不到就以 chunk 数近似（标注 estimated）
    out_tokens = int((usage or {}).get("completion_tokens") or 0)
    estimated = out_tokens <= 0
    if estimated:
        out_tokens = chunks

    # 第一个 token 的耗时已经算进 TTFT，解码阶段只对剩余的 token 计速
    decode_tps = None
    if decode_ms > 0 and out_tokens > 1:
        decode_tps = round((out_tokens - 1) / (decode_ms / 1000), 2)

    overall_tps = round(out_tokens / (wall_ms / 1000), 2) if wall_ms > 0 and out_tokens > 0 else None

    return {
        "status": "ok",
        "code": 200,
        "ttft_ms": ttft_ms,
        "decode_ms": decode_ms,
        "wall_ms": wall_ms,
        "output_tokens": out_tokens,
        "decode_tps": decode_tps,
        "overall_tps": overall_tps,
        "estimated": estimated,
    }


async def measure_stream_speed(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    aliases: Optional[dict] = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """测一个模型的真实生成速度。"""
    from services.health_service import normalize_model_name

    actual_model = normalize_model_name((aliases or {}).get(model, model))

    result = await _stream_once(client, base_url, api_key, actual_model, max_tokens, True)
    if result.get("status") == "retry_no_usage_opt":
        result = await _stream_once(client, base_url, api_key, actual_model, max_tokens, False)
    return result


def _prune_runs() -> None:
    """只保留最近 KEEP_RUNS 个任务。"""
    if len(_RUNS) <= KEEP_RUNS:
        return
    ordered = sorted(_RUNS.items(), key=lambda kv: kv[1].get("started_at", 0))
    for run_id, _ in ordered[: len(_RUNS) - KEEP_RUNS]:
        _RUNS.pop(run_id, None)


def get_run(run_id: str) -> Optional[dict]:
    return _RUNS.get(run_id)


async def run_speed_test(
    run_id: str,
    items: list[tuple[str, str]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> None:
    """后台任务：并发探测 items 里的 (provider, model)，逐个落库并更新内存进度。"""
    from database.engine import db
    from database.models import Provider, SpeedRecord
    from services.meta_service import ModelMetaService

    run = _RUNS[run_id]
    aliases = ModelMetaService().aliases
    sem = asyncio.Semaphore(CONCURRENCY)

    async with db.SessionLocal() as session:
        providers = {
            p.name: p for p in (await session.execute(select(Provider))).scalars().all()
        }

    async def measure_one(provider_name: str, model: str) -> dict:
        p = providers.get(provider_name)
        if p is None:
            return {"provider": provider_name, "model": model,
                    "status": "error", "error": "提供商已不存在"}

        async with sem:
            try:
                async with httpx.AsyncClient(verify=settings.load().verify_ssl) as client:
                    res = await measure_stream_speed(
                        client, p.base_url, p.api_key, model, aliases, max_tokens
                    )
            except Exception as e:
                res = {"status": "error", "error": f"{type(e).__name__}: {e}"[:300]}

        row = {
            "provider": provider_name,
            "model": model,
            "status": res.get("status", "error"),
            "ttft_ms": res.get("ttft_ms"),
            "decode_tps": res.get("decode_tps"),
            "overall_tps": res.get("overall_tps"),
            "output_tokens": res.get("output_tokens") or 0,
            "decode_ms": res.get("decode_ms"),
            "wall_ms": res.get("wall_ms"),
            "estimated": bool(res.get("estimated")),
            "error": res.get("error") or "",
        }

        # 逐条落库：中途被打断也不丢已完成的结果
        try:
            async with db.SessionLocal() as session:
                session.add(SpeedRecord(
                    run_id=run_id,
                    provider_name=provider_name,
                    model=model,
                    status=row["status"],
                    ttft_ms=row["ttft_ms"],
                    decode_tps=row["decode_tps"],
                    overall_tps=row["overall_tps"],
                    output_tokens=row["output_tokens"],
                    decode_ms=row["decode_ms"],
                    wall_ms=row["wall_ms"],
                    estimated=row["estimated"],
                    error=row["error"] or None,
                    tested_at=datetime.now(),
                ))
                await session.commit()
        except Exception as e:
            logger.warning("测速结果落库失败 %s|%s: %s", provider_name, model, e)

        return row

    try:
        tasks = [measure_one(p, m) for p, m in items]
        for coro in asyncio.as_completed(tasks):
            row = await coro
            run["results"].append(row)
            run["done"] = len(run["results"])
            logger.info(
                "[speed %s] %s/%s %s ttft=%sms decode=%s t/s (%.1f%%)",
                run_id, row["provider"], row["model"], row["status"],
                row["ttft_ms"], row["decode_tps"], run["done"] / max(1, run["total"]) * 100,
            )
    except asyncio.CancelledError:
        run["error"] = "任务被取消"
        raise
    except Exception as e:
        logger.exception("测速任务异常")
        run["error"] = f"{type(e).__name__}: {e}"
    finally:
        run["finished"] = True
        run["finished_at"] = time.time()


def create_run(items: list[tuple[str, str]], max_tokens: int, skipped: int = 0) -> str:
    """登记一个新任务并起后台协程，返回 run_id。"""
    run_id = uuid.uuid4().hex[:8]
    _RUNS[run_id] = {
        "run_id": run_id,
        "started_at": time.time(),
        "finished_at": None,
        "finished": False,
        "total": len(items),
        "done": 0,
        "results": [],
        "skipped": skipped,
        "max_tokens": max_tokens,
        "error": "",
    }
    _prune_runs()
    asyncio.create_task(run_speed_test(run_id, items, max_tokens))
    return run_id
