"""OpenAI Responses API adapter built on the existing Chat Completions proxy."""
import json
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .proxy import proxy_chat, verify_client

logger = logging.getLogger("flowgate.responses")
router = APIRouter(prefix="/v1")


def _content_to_chat(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise HTTPException(400, "input content must be a string or array")
    parts = []
    for item in content:
        if not isinstance(item, dict):
            raise HTTPException(400, "invalid input content item")
        kind = item.get("type")
        # output_texto is a non-standard variant emitted by some clients; accept it
        # alongside the official Responses input_text/output_text content types.
        if kind in ("input_text", "output_text", "output_texto", "text"):
            parts.append({"type": "text", "text": item.get("text", "")})
        elif kind == "input_image":
            image = item.get("image_url") or item.get("file_id")
            if not isinstance(image, str):
                raise HTTPException(400, "input_image requires a URL; file_id is unsupported")
            parts.append({"type": "image_url", "image_url": {"url": image}})
        else:
            raise HTTPException(400, f"unsupported input content type: {kind}")
    return parts


def _request_to_chat(body: dict) -> dict:
    unsupported = ("previous_response_id", "conversation")
    if any(body.get(key) for key in unsupported):
        raise HTTPException(400, "stateful Responses fields are not supported")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(400, "model is required")
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})
    input_data = body.get("input")
    if isinstance(input_data, str):
        messages.append({"role": "user", "content": input_data})
    elif isinstance(input_data, list):
        for item in input_data:
            if not isinstance(item, dict):
                raise HTTPException(400, "input items must be objects")
            kind = item.get("type", "message")
            if kind == "message":
                role = item.get("role", "user")
                if role not in ("system", "developer", "user", "assistant"):
                    raise HTTPException(400, f"unsupported message role: {role}")
                messages.append({"role": role, "content": _content_to_chat(item.get("content", ""))})
            elif kind == "function_call_output":
                messages.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": str(item.get("output", ""))})
            elif kind == "function_call":
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": item.get("call_id"), "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments", "{}")},
                }]})
            else:
                raise HTTPException(400, f"unsupported input item type: {kind}")
    else:
        raise HTTPException(400, "input is required and must be a string or array")

    result = {"model": model, "messages": messages, "stream": bool(body.get("stream", False))}
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"),
                           ("max_output_tokens", "max_tokens"), ("tools", "tools"),
                           ("tool_choice", "tool_choice"), ("parallel_tool_calls", "parallel_tool_calls"),
                           ("top_logprobs", "top_logprobs"), ("user", "user")):
        if source in body:
            result[target] = body[source]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        if isinstance(reasoning.get("effort"), str):
            result["reasoning_effort"] = reasoning["effort"]
        if isinstance(reasoning.get("summary"), str):
            result["reasoning_summary"] = reasoning["summary"]
    if isinstance(result.get("tools"), list):
        converted = []
        for tool in result["tools"]:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise HTTPException(400, "only function tools are supported")
            converted.append({"type": "function", "function": {
                key: tool[key] for key in ("name", "description", "parameters", "strict") if key in tool
            }})
        result["tools"] = converted
    text_format = (body.get("text") or {}).get("format") if isinstance(body.get("text"), dict) else None
    if isinstance(text_format, dict):
        if text_format.get("type") == "json_object":
            result["response_format"] = {"type": "json_object"}
        elif text_format.get("type") == "json_schema":
            schema = {key: text_format[key] for key in ("name", "schema", "strict") if key in text_format}
            result["response_format"] = {"type": "json_schema", "json_schema": schema}
    return result


def _request_summary(body: dict) -> tuple[int, int]:
    """Return input item and text-character counts without logging prompt content."""
    input_data = body.get("input")
    if isinstance(input_data, str):
        return 1, len(input_data)
    if not isinstance(input_data, list):
        return 0, 0
    text_chars = 0
    for item in input_data:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if isinstance(content, str):
            text_chars += len(content)
        elif isinstance(content, list):
            text_chars += sum(
                len(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return len(input_data), text_chars


class _BodyRequest:
    """Minimal request facade so the existing proxy handler can be reused directly."""
    def __init__(self, body: dict):
        self.body = body

    async def json(self) -> dict:
        return self.body


def _usage_to_response(usage: dict | None) -> dict | None:
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", 0) or 0
    completion = usage.get("completion_tokens", 0) or 0
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": prompt_details.get("cached_tokens", 0) or 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": completion_details.get("reasoning_tokens", 0) or 0},
        "total_tokens": usage.get("total_tokens", prompt + completion) or 0,
    }


def _response_envelope(model: str, *, response_id: str | None = None,
                       created_at: int | None = None, status: str = "completed",
                       output: list | None = None, usage: dict | None = None,
                       incomplete_reason: str | None = None) -> dict:
    """Build the stable Responses object shape used by DeepSeek's native API."""
    return {
        "id": response_id or "resp_" + uuid.uuid4().hex,
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": {"reason": incomplete_reason} if incomplete_reason else None,
        "model": model,
        "output": output or [],
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "instructions": None,
        "max_output_tokens": None,
        "metadata": {},
        "reasoning": {"effort": None, "summary": None},
        "store": False,
        "temperature": None,
        "text": {"format": {"type": "text"}, "verbosity": "medium"},
        "tool_choice": "auto",
        "tools": [],
        "top_p": None,
        "truncation": "disabled",
        "usage": usage,
        "user": None,
    }


def _error_response(exc: HTTPException) -> JSONResponse:
    """Return OpenAI-compatible error JSON instead of FastAPI's {detail: ...}."""
    status_code = exc.status_code
    error_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
    }.get(status_code, "server_error" if status_code >= 500 else "invalid_request_error")
    detail = exc.detail
    message = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return JSONResponse(
        {"error": {"message": message, "type": error_type,
                   "code": error_type, "param": None}},
        status_code=status_code,
    )


def _chat_to_response(chat: dict, model: str, response_id: str | None = None) -> dict:
    choice = (chat.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append({"id": "msg_" + uuid.uuid4().hex[:24], "type": "message",
                       "status": "completed", "role": "assistant",
                       "content": [{"type": "output_text", "text": content, "annotations": []}]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        output.append({"id": call.get("id") or "fc_" + uuid.uuid4().hex[:24],
                       "type": "function_call", "status": "completed",
                       "call_id": call.get("id"), "name": fn.get("name", ""),
                       "arguments": fn.get("arguments", "{}")})
    finish = choice.get("finish_reason")
    incomplete_reason = {
        "length": "max_output_tokens",
        "content_filter": "content_filter",
    }.get(finish)
    status = "incomplete" if incomplete_reason else "completed"
    if status == "incomplete":
        for item in output:
            if item.get("type") == "message":
                item["status"] = status
    if not output:
        output.append({
            "id": "msg_" + uuid.uuid4().hex[:24],
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [{"type": "output_text", "text": content if isinstance(content, str) else "", "annotations": []}],
        })
    usage = _usage_to_response(chat.get("usage"))
    result = _response_envelope(
        chat.get("model", model), response_id=response_id, status=status,
        output=output, usage=usage,
        incomplete_reason=incomplete_reason,
    )
    result["output_text"] = "".join(
        part["text"] for item in output if item["type"] == "message"
        for part in item["content"] if part["type"] == "output_text"
    )
    return result


@router.post("/responses", dependencies=[Depends(verify_client)])
async def proxy_responses(request: Request):
    model = "unknown"
    try:
        raw_body = await request.json()
        if not isinstance(raw_body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        input_items, input_chars = _request_summary(raw_body)
        model = raw_body.get("model") if isinstance(raw_body.get("model"), str) else "unknown"
        body = _request_to_chat(raw_body)
        logger.info(
            "Responses request: model=%s stream=%s input_items=%d input_chars=%d max_output_tokens=%s reasoning_effort=%s",
            body["model"], body["stream"], input_items, input_chars,
            body.get("max_tokens", "default"), body.get("reasoning_effort", "default"),
        )
        upstream = await proxy_chat(_BodyRequest(body))
    except HTTPException as exc:
        logger.warning("Responses request rejected: status=%d error=%s", exc.status_code, exc.detail)
        return _error_response(exc)
    except Exception as exc:
        logger.exception("Responses request failed before response conversion: model=%s", model)
        return _error_response(HTTPException(502, f"Responses proxy failed: {type(exc).__name__}"))
    if isinstance(upstream, StreamingResponse):
        logger.info("Responses stream opened: model=%s", body["model"])
        return StreamingResponse(_stream_to_responses(upstream, body["model"]),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    if isinstance(upstream, JSONResponse):
        try:
            chat = json.loads(upstream.body)
        except (ValueError, TypeError):
            logger.warning("Responses proxy returned non-JSON body: model=%s status=%d body_chars=%d",
                           body["model"], upstream.status_code, len(upstream.body or b""))
            return upstream
        if not isinstance(chat, dict) or "choices" not in chat:
            logger.warning("Responses upstream returned unexpected body: model=%s", body["model"])
            return upstream
        result = _chat_to_response(chat, body["model"])
        choice = (chat.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        output_text = result.get("output_text", "")
        usage = result.get("usage") or {}
        logger.info(
            "Responses completed: id=%s model=%s status=%s finish_reason=%s output_items=%d output_chars=%d tool_calls=%d input_tokens=%s output_tokens=%s",
            result["id"], result["model"], result["status"], choice.get("finish_reason"),
            len(result["output"]), len(output_text), len(message.get("tool_calls") or []),
            usage.get("input_tokens", "unknown"), usage.get("output_tokens", "unknown"),
        )
        if not output_text and not message.get("tool_calls"):
            reasoning = message.get("reasoning_content")
            logger.warning(
                "Responses has no visible assistant text: model=%s finish_reason=%s reasoning_chars=%d; check token limit/reasoning mode and upstream output",
                result["model"], choice.get("finish_reason"),
                len(reasoning) if isinstance(reasoning, str) else 0,
            )
        return JSONResponse(result, status_code=upstream.status_code)
    return upstream


async def _stream_to_responses(upstream: StreamingResponse, model: str):
    rid = "resp_" + uuid.uuid4().hex
    created = int(time.time())
    text = ""
    item_id = "msg_" + uuid.uuid4().hex[:24]
    sequence_number = 0

    def event(name: str, data: dict) -> bytes:
        nonlocal sequence_number
        data["sequence_number"] = sequence_number
        sequence_number += 1
        return (f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n").encode()

    response = _response_envelope(model, response_id=rid, created_at=created,
                                  status="in_progress", output=[], usage=None)

    yield event("response.created", {"type": "response.created", "response": response})
    yield event("response.in_progress", {"type": "response.in_progress", "response": response})
    item = {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
    yield event("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": item})
    part = {"type": "output_text", "text": "", "annotations": []}
    yield event("response.content_part.added", {"type": "response.content_part.added", "item_id": item_id, "output_index": 0, "content_index": 0, "part": part})

    buffer = ""
    finish = None
    usage = None
    chunks_seen = 0
    saw_done = False
    reasoning_chars = 0
    stream_error = None
    MAX_BUFFER_SIZE = 1024 * 1024  # 1MB 上限，防止无换行符导致 buffer 无限增长
    # 前缀过滤状态：proxy 会在流式第一个事件预置 "🍀 {provider} - {model}🌿🎋\n\n"，
    # 可能被分割到多个 chunk，需跨 chunk 累积后再过滤。
    prefix_buffer = ""
    prefix_done = False
    try:
        async for chunk in upstream.body_iterator:
            buffer += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            # 防御：若 buffer 超过上限且仍无换行符，强制截断避免内存泄漏
            if len(buffer) > MAX_BUFFER_SIZE and "\n" not in buffer:
                logger.warning("Responses stream buffer exceeded %d bytes without newline, truncating: model=%s", MAX_BUFFER_SIZE, model)
                buffer = buffer[-MAX_BUFFER_SIZE:]
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    saw_done = True
                    continue
                try:
                    chunk_obj = json.loads(payload)
                except json.JSONDecodeError:
                    logger.warning("Invalid Chat Completions SSE JSON: model=%s payload_chars=%d", model, len(payload))
                    continue
                chunks_seen += 1
                if isinstance(chunk_obj.get("usage"), dict):
                    usage = _usage_to_response(chunk_obj["usage"])
                choices = chunk_obj.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                for key in ("reasoning_content", "reasoning"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        reasoning_chars += len(value)
                details = delta.get("reasoning_details")
                if isinstance(details, list):
                    reasoning_chars += sum(
                        len(part.get("text", ""))
                        for part in details
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
                fragment = delta.get("content")
                if not isinstance(fragment, str) or not fragment:
                    continue
                # The existing proxy prepends a provider banner to each chat stream; omit it from API text.
                # 可能被分割到多个 chunk，需跨 chunk 累积后再过滤。
                if not text and not prefix_done:
                    prefix_buffer += fragment
                    if prefix_buffer.startswith("🍀 ") and "🌿🎋\n\n" in prefix_buffer:
                        idx = prefix_buffer.find("🌿🎋\n\n")
                        fragment = prefix_buffer[idx + len("🌿🎋\n\n"):]
                        prefix_done = True
                    else:
                        # 尚未看到完整前缀，继续等待；防止无限累积
                        if len(prefix_buffer) > 200:
                            prefix_done = True
                            fragment = prefix_buffer
                            prefix_buffer = ""
                        else:
                            continue
                if not fragment:
                    continue
                text += fragment
                yield event("response.output_text.delta", {"type": "response.output_text.delta", "item_id": item_id,
                                                            "output_index": 0, "content_index": 0, "delta": fragment})
    except Exception as exc:
        stream_error = exc
        logger.exception(
            "Responses upstream stream failed: id=%s model=%s chunks=%d output_chars=%d",
            rid, model, chunks_seen, len(text),
        )
    if buffer.strip():
        logger.warning("Responses stream ended with an incomplete SSE frame: id=%s model=%s buffered_chars=%d",
                       rid, model, len(buffer))
    output = [{"id": item_id, "type": "message", "status": "completed", "role": "assistant",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}]
    incomplete_reason = {
        "length": "max_output_tokens",
        "content_filter": "content_filter",
    }.get(finish)
    status = "failed" if stream_error else "incomplete" if incomplete_reason else "completed"
    output[0]["status"] = status
    completed = _response_envelope(
        model, response_id=rid, created_at=created, status=status,
        output=output, usage=usage,
        incomplete_reason=incomplete_reason,
    )
    completed["output_text"] = text
    if stream_error:
        completed["error"] = {"type": "server_error", "code": "upstream_stream_error",
                              "message": "The upstream stream ended unexpectedly."}
    yield event("response.output_text.done", {"type": "response.output_text.done", "item_id": item_id,
                                               "output_index": 0, "content_index": 0, "text": text})
    yield event("response.content_part.done", {"type": "response.content_part.done", "item_id": item_id,
                                                "output_index": 0, "content_index": 0,
                                                "part": output[0]["content"][0]})
    yield event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": output[0]})
    terminal_event = "response.failed" if status == "failed" else "response.incomplete" if status == "incomplete" else "response.completed"
    yield event(terminal_event, {"type": terminal_event, "response": completed})
    if text:
        logger.info(
            "Responses stream completed: id=%s model=%s status=%s finish_reason=%s chunks=%d output_chars=%d usage=%s done_marker=%s",
            rid, model, status, finish, chunks_seen, len(text), usage or "unavailable", saw_done,
        )
    else:
        logger.warning(
            "Responses stream has no visible text: id=%s model=%s status=%s finish_reason=%s chunks=%d reasoning_chars=%d usage=%s done_marker=%s",
            rid, model, status, finish, chunks_seen, reasoning_chars, usage or "unavailable", saw_done,
        )
