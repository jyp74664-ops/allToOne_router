"""多协议入口：Anthropic Messages / Gemini generateContent / Embeddings 透传。

思路统一：把别的协议翻译成 OpenAI chat 格式，直接复用 proxy_chat 那套
「候选筛选 + 失败 fallback + 用量记账 + 请求日志」，再把响应翻译回去。
不重复实现路由逻辑，也不用走本机回环 HTTP。
"""
import json
import logging
import time
import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from .proxy import (
    _upstream_client,
    normalize_router_name,
    proxy_chat,
    verify_client,
)
from services.observability import new_trace_id, record_request
from services.usage_service import append_usage

logger = logging.getLogger("flowgate.protocols")
router = APIRouter()


class _BodyRequest:
    """把 dict 包成 proxy_chat 需要的 request 形状（带鉴权归属）。"""

    def __init__(self, body: dict, state=None):
        self.body = body
        self.state = state if state is not None else SimpleNamespace()

    async def json(self) -> dict:
        return self.body


# ------------------------------------------------------------
# Anthropic Messages
# ------------------------------------------------------------
def _content_to_openai(content):
    """Anthropic content（str / blocks）→ OpenAI content（str / parts）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for blk in content:
        if not isinstance(blk, dict):
            continue
        kind = blk.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": blk.get("text", "")})
        elif kind == "image":
            src = blk.get("source") or {}
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
            else:
                url = src.get("url", "")
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
        elif kind == "tool_result":
            inner = blk.get("content")
            text = inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
            parts.append({"type": "text", "text": text})
        elif kind == "tool_use":
            parts.append({
                "type": "text",
                "text": json.dumps(blk.get("input") or {}, ensure_ascii=False),
            })
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0]["text"]
    return parts or ""


def _tools_to_openai(tools) -> list:
    out = []
    for t in tools or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return out


def _anthropic_to_chat(body: dict) -> dict:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "model is required")
    messages = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(b.get("text", "") for b in system if isinstance(b, dict))
        if text:
            messages.append({"role": "system", "content": text})

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": _content_to_openai(m.get("content"))})
    if not messages:
        raise HTTPException(400, "messages is required")

    chat = {"model": model, "messages": messages, "stream": bool(body.get("stream"))}
    for src, dst in (
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
    ):
        if body.get(src) is not None:
            chat[dst] = body[src]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"]

    tools = _tools_to_openai(body.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "any":
            chat["tool_choice"] = "required"
        elif kind == "tool":
            chat["tool_choice"] = {"type": "function", "function": {"name": choice.get("name")}}
        elif kind == "none":
            chat["tool_choice"] = "none"
    return chat


def _chat_to_anthropic(chat: dict, model: str) -> dict:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = []
    text = msg.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (ValueError, TypeError):
            args = {"raw": raw}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or "toolu_" + uuid.uuid4().hex[:20],
            "name": fn.get("name", ""),
            "input": args,
        })
    if not content:
        content = [{"type": "text", "text": ""}]

    stop_reason = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "stop_sequence",
    }.get(choice.get("finish_reason"), "end_turn")
    usage = chat.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:20],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
        },
    }


def _strip_banner(text: str) -> str:
    """去掉 proxy 在流开头插的「🍀 渠道 - 模型」横幅，Anthropic 客户端不需要。"""
    if text.startswith("🍀") and "🌿🎋" in text:
        idx = text.find("🌿🎋")
        return text[idx + len("🌿🎋"):].lstrip("\r\n")
    return text


async def _stream_anthropic(upstream: StreamingResponse, model: str):
    """OpenAI SSE → Anthropic SSE。

    文本增量实时转发；tool_calls 的增量先累积，在文本结束后一次性发
    tool_use block（Claude Code 这类客户端在 message_stop 前收到即可）。
    """
    msg_id = "msg_" + uuid.uuid4().hex[:20]
    yield "event: message_start\ndata: " + json.dumps({
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }, ensure_ascii=False) + "\n\n"
    yield "event: content_block_start\ndata: " + json.dumps({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }, ensure_ascii=False) + "\n\n"

    started_text = False
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage = {}

    async for chunk in upstream.body_iterator:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        for line in text.split("\n"):
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                piece = _strip_banner(piece)
                if piece:
                    started_text = True
                    yield "event: content_block_delta\ndata: " + json.dumps({
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    }, ensure_ascii=False) + "\n\n"
            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get("index") or 0)
                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]

    if started_text:
        yield "event: content_block_stop\ndata: " + json.dumps({
            "type": "content_block_stop", "index": 0,
        }, ensure_ascii=False) + "\n\n"

    block_index = 1
    for idx in sorted(tool_calls):
        slot = tool_calls[idx]
        if not slot["name"]:
            continue
        try:
            args = json.loads(slot["args"] or "{}")
        except ValueError:
            args = {"raw": slot["args"]}
        yield "event: content_block_start\ndata: " + json.dumps({
            "type": "content_block_start", "index": block_index,
            "content_block": {
                "type": "tool_use",
                "id": slot["id"] or "toolu_" + uuid.uuid4().hex[:20],
                "name": slot["name"], "input": {},
            },
        }, ensure_ascii=False) + "\n\n"
        yield "event: content_block_delta\ndata: " + json.dumps({
            "type": "content_block_delta", "index": block_index,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(args, ensure_ascii=False)},
        }, ensure_ascii=False) + "\n\n"
        yield "event: content_block_stop\ndata: " + json.dumps({
            "type": "content_block_stop", "index": block_index,
        }, ensure_ascii=False) + "\n\n"
        block_index += 1

    stop_reason = {
        "stop": "end_turn", "length": "max_tokens",
        "tool_calls": "tool_use", "content_filter": "stop_sequence",
    }.get(finish_reason, "end_turn")
    yield "event: message_delta\ndata: " + json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": usage.get("completion_tokens", 0) or 0},
    }, ensure_ascii=False) + "\n\n"
    yield "event: message_stop\ndata: " + json.dumps({"type": "message_stop"}) + "\n\n"


@router.post("/v1/messages", dependencies=[Depends(verify_client)])
async def anthropic_messages(request: Request):
    """Anthropic Messages API（Claude 客户端可直接接入）。"""
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    chat = _anthropic_to_chat(body)
    model = chat["model"]
    started = time.time()
    try:
        upstream = await proxy_chat(_BodyRequest(chat, request.state))
    except HTTPException as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    if isinstance(upstream, StreamingResponse):
        return StreamingResponse(
            _stream_anthropic(upstream, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if isinstance(upstream, JSONResponse):
        try:
            data = json.loads(upstream.body)
        except (ValueError, TypeError):
            return upstream
        if isinstance(data, dict) and data.get("error"):
            return JSONResponse(data, status_code=upstream.status_code)
        logger.info("Anthropic request done: model=%s %.2fs", model, time.time() - started)
        return JSONResponse(_chat_to_anthropic(data, model), status_code=upstream.status_code)
    return upstream


# ------------------------------------------------------------
# Gemini generateContent
# ------------------------------------------------------------
def _gemini_to_chat(body: dict, model: str) -> dict:
    messages = []
    system = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(system, dict):
        text = "".join(
            p.get("text", "") for p in system.get("parts") or [] if isinstance(p, dict)
        )
        if text:
            messages.append({"role": "system", "content": text})

    for c in body.get("contents") or []:
        if not isinstance(c, dict):
            continue
        role = "assistant" if c.get("role") == "model" else "user"
        text = "".join(
            p.get("text", "") for p in c.get("parts") or [] if isinstance(p, dict)
        )
        if text:
            messages.append({"role": role, "content": text})
    if not messages:
        raise HTTPException(400, "contents is required")

    gen = body.get("generationConfig") or body.get("generation_config") or {}
    chat = {"model": model, "messages": messages}
    if gen.get("maxOutputTokens"):
        chat["max_tokens"] = gen["maxOutputTokens"]
    if gen.get("temperature") is not None:
        chat["temperature"] = gen["temperature"]
    if gen.get("topP") is not None:
        chat["top_p"] = gen["topP"]
    if gen.get("stopSequences"):
        chat["stop"] = gen["stopSequences"]
    return chat


def _chat_to_gemini(chat: dict, model: str) -> dict:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts = []
    if isinstance(msg.get("content"), str) and msg["content"]:
        parts.append({"text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
    finish = {
        "stop": "STOP", "length": "MAX_TOKENS",
        "tool_calls": "STOP", "content_filter": "SAFETY",
    }.get(choice.get("finish_reason"), "STOP")
    usage = chat.get("usage") or {}
    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts or [{"text": ""}]},
            "finishReason": finish,
            "index": 0,
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0) or 0,
            "candidatesTokenCount": usage.get("completion_tokens", 0) or 0,
            "totalTokenCount": usage.get("total_tokens", 0) or 0,
        },
        "modelVersion": model,
    }


@router.post("/v1beta/models/{path:path}", dependencies=[Depends(verify_client)])
async def gemini_generate(path: str, request: Request):
    """Gemini generateContent（非流式）。

    path 里带着 ":generateContent" 后缀，用 catch-all 接住再拆——
    FastAPI 的普通路径参数会把冒号一起吞进参数值。
    """
    if not path.endswith(":generateContent"):
        raise HTTPException(
            404,
            "只支持 :generateContent；流式 :streamGenerateContent 暂未实现",
        )
    model = path[: -len(":generateContent")]
    if model.startswith("models/"):
        model = model[len("models/"):]
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")

    chat = _gemini_to_chat(body, model)
    upstream = await proxy_chat(_BodyRequest(chat, request.state))
    if isinstance(upstream, JSONResponse):
        try:
            data = json.loads(upstream.body)
        except (ValueError, TypeError):
            return upstream
        if isinstance(data, dict) and data.get("error"):
            return JSONResponse(data, status_code=upstream.status_code)
        return JSONResponse(_chat_to_gemini(data, model), status_code=upstream.status_code)
    return upstream


# ------------------------------------------------------------
# Embeddings 透传
# ------------------------------------------------------------
@router.post("/v1/embeddings", dependencies=[Depends(verify_client)])
async def proxy_embeddings(request: Request):
    """向量模型透传。

    向量请求走不了 chat/completions（原来的 fetch_models 直接把 embed 模型
    过滤掉了），这里按 model 找有它的渠道直接转发，失败换下一家。
    """
    from config.settings import settings
    from database.engine import db
    from database.models import Provider

    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "请求缺少 model")
    model = await normalize_router_name(model)

    async with db.SessionLocal() as session:
        providers = (await session.execute(select(Provider))).scalars().all()
    candidates = [
        p for p in providers
        if model in (p.models or []) and model not in (p.disabled_models or [])
    ]
    if not candidates:
        raise HTTPException(404, f"没有提供商提供模型 {model}（embedding 模型需先在模型列表里保留）")

    cfg = settings.load()
    trace_id = new_trace_id()
    started = time.time()
    last_error = "无可用渠道"
    async with _upstream_client(bool(cfg.verify_ssl)) as (client, _shared):
        for p in candidates:
            url = p.base_url.rstrip("/") + "/embeddings"
            payload = dict(body)
            payload["model"] = model
            try:
                resp = await client.post(url, json=payload, headers={
                    "Authorization": f"Bearer {p.api_key}",
                    "Content-Type": "application/json",
                })
            except Exception as exc:
                last_error = f"{p.name}: {exc}"
                continue
            if resp.status_code >= 400:
                last_error = f"{p.name}: HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except ValueError:
                last_error = f"{p.name}: 非 JSON 响应"
                continue
            usage = data.get("usage") or {}
            pt = usage.get("prompt_tokens", 0) or 0
            await append_usage(
                provider=p.name, model=model,
                prompt_tokens=pt, completion_tokens=0,
                total_tokens=usage.get("total_tokens", 0) or pt,
                upstream_model=model,
            )
            await record_request(
                trace_id=trace_id, requested_model=model, endpoint="/v1/embeddings",
                provider=p.name, upstream_model=model, stream=False, ok=True,
                status_code=resp.status_code, attempts=1,
                latency_ms=int((time.time() - started) * 1000),
                prompt_tokens=pt, api_key_name=getattr(request.state, "api_key_name", ""),
            )
            return JSONResponse(data, status_code=resp.status_code)

    await record_request(
        trace_id=trace_id, requested_model=model, endpoint="/v1/embeddings",
        stream=False, ok=False, status_code=503,
        attempts=len(candidates), latency_ms=int((time.time() - started) * 1000),
        api_key_name=getattr(request.state, "api_key_name", ""), error=last_error,
    )
    raise HTTPException(503, f"所有提供 {model} 的渠道均不可用: {last_error}")
