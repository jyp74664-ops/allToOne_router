"""代理接口路由（OpenAI 兼容）"""
import json
import time
import logging
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
import httpx

from config.settings import settings
from services.health_service import health_state, RATE_LIMIT_STATUS_CODES
from services.router_manager import ROUTERS
from services.usage_service import append_usage

logger = logging.getLogger("flowgate.proxy")
UPSTREAM_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/v1")


def verify_client(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """客户端鉴权"""
    if not credentials or credentials.credentials != settings.load().local_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return credentials.credentials


# Hermes 工具名压缩 / 还原
HERMES_MAP = [
    ("mcp_hermes_studio_use_hermes_studio_use_", "mcp_hermes_studio_use_hermes_studio_use_"),
    ("mcp_hermes_studio_devices_hermes_studio_lan_", "mcp_hermes_studio_devices_hermes_studio_lan_"),
    ("mcp_hermes_studio_api_hermes_studio_api_", "mcp_hermes_studio_api_hermes_studio_api_"),
]


def compress_hermes(obj: dict) -> dict:
    s = json.dumps(obj, ensure_ascii=False)
    for long, short in HERMES_MAP:
        s = s.replace(long, short)
    return json.loads(s)


def restore_hermes_text(text: str) -> str:
    for long, short in HERMES_MAP:
        text = text.replace(short, long)
    return text


def merge_reasoning(obj: dict) -> dict:
    """合并 reasoning_content"""
    choices = obj.get("choices")
    if not choices or not isinstance(choices, list):
        return obj
    for choice in choices:
        target = choice.get("delta") or choice.get("message")
        if not target or not isinstance(target, dict):
            continue
        rc = target.pop("reasoning_content", None)
        if rc is None:
            continue
        c = target.get("content")
        if isinstance(c, str) and c:
            target["content"] = rc + c
        else:
            target["content"] = rc
    return obj


# 回复语言跟随
LANG_HINTS = {
    "zh": (
        "\n\n【重要】请始终使用简体中文回答用户。"
        "思考过程(reasoning)也请用中文。"
        "代码、命令、文件名、专有名词、标识符等保持原样即可，不要翻译。"
    ),
    "en": (
        "\n\n[Important] Please always respond to the user in English. "
        "The reasoning process should also be in English. "
        "Keep code, commands, file names, proper nouns, and identifiers as-is; do not translate them."
    ),
}


def normalize_model_name(model: str) -> str:
    """去除常见前缀，使模型名适合发送到上游 API。"""
    return model.removeprefix("models/")


def detect_user_lang(msgs: list) -> str:
    """检测用户消息语言"""
    for m in reversed(msgs):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(
                seg.get("text", "")
                for seg in c
                if isinstance(seg, dict) and seg.get("type") == "text"
            )
        if not isinstance(c, str):
            continue
        cjk = sum(1 for ch in c if "\u4e00" <= ch <= "\u9fff")
        lat = sum(1 for ch in c if ch.isascii() and ch.isalpha())
        if cjk == 0 and lat == 0:
            continue
        return "zh" if cjk >= lat else "en"
    return "zh"


def ensure_lang_reply(body: dict) -> dict:
    """注入回复语言提示"""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body
    lang = detect_user_lang(msgs)
    hint = LANG_HINTS['zh']
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        c = first.get("content")
        if isinstance(c, str) and "请始终使用简体中文" not in c and "always respond to the user in English" not in c:
            first["content"] = c.rstrip() + hint
        return body
    sys_text = ("请使用简体中文回答。" + hint) if lang == "zh" else ("Please respond in English. " + hint)
    msgs.insert(0, {"role": "system", "content": sys_text})
    return body


# 路由组英文名称映射（客户端常用英文名 -> 数据库中的中文名）
ROUTER_NAME_ALIAS = {
    "daily-general-fast": "日常通用 / 快速响应 / 低成本",
    "daily-general": "日常通用 / 快速响应 / 低成本",
    "image-analysis": "图文分析 / 视觉任务",
    "visual-task": "图文分析 / 视觉任务",
    "chinese-long-context": "中文长文本 / 角色扮演 / 复杂对话",
    "roleplay": "中文长文本 / 角色扮演 / 复杂对话",
    "lin-ruoxi": "林若曦",
    "speed": "速度型选手",
    "large-model": "超大模型",
    "my-auto-model": "MyAutoModel",
}


def normalize_router_name(name: str) -> str:
    """规范化路由组名称，支持英文别名"""
    return ROUTER_NAME_ALIAS.get(name.lower().strip(), name)


@router.api_route("/chat/completions", methods=["POST"], dependencies=[Depends(verify_client)])
async def proxy_chat(request: Request, force: bool = False):
    """代理聊天补全请求"""
    body = await request.json()
    body = compress_hermes(body)
    body = ensure_lang_reply(body)
    requested_model = body.get("model")
    # 规范化路由组名称（支持英文别名）
    requested_model = normalize_router_name(requested_model)
    
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(400, "请求缺少 model")

    # 路由名展开为模型优先级列表；普通模型名则只匹配自身。
    from database.engine import db
    from database.models import Provider, RouterGroup
    from services.meta_service import ModelMetaService
    meta = ModelMetaService()
    async with db.SessionLocal() as session:
        router_result = await session.execute(
            select(RouterGroup).where(RouterGroup.name == requested_model)
        )
        router = router_result.scalar_one_or_none()
        wanted_models = router.models if router else [requested_model]
        provider_result = await session.execute(select(Provider))
        providers = provider_result.scalars().all()

    # 兼容 "/v1/models" 返回的 "{provider}-{model}" 前缀 id 格式（如 nvidia-01-ai/yi-large）
    if router is None:
        for provider in providers:
            prefix = f"{provider.name}-"
            if requested_model.startswith(prefix):
                rest = requested_model[len(prefix):]
                if rest in (provider.models or []):
                    wanted_models = [rest]
                    providers = [provider]
                break

    candidates = []
    for wanted in wanted_models:
        actual_model = normalize_model_name(meta.aliases.get(wanted, wanted))
        for provider in providers:
            # 匹配逻辑：检查 wanted 或 actual_model 是否在 provider.models 中
            # 同时也检查 aliases 中是否有映射到 wanted 的键（如 "ZhipuAI/GLM-5.2" -> "glm-5.2"）
            model_in_list = wanted in (provider.models or []) or actual_model in (provider.models or [])
            # 检查 aliases 反向映射：provider.models 中的名称是否映射到 wanted
            if not model_in_list:
                for provider_model in (provider.models or []):
                    if meta.aliases.get(provider_model) == wanted:
                        model_in_list = True
                        break
            if not model_in_list:
                continue
            if wanted in (provider.disabled_models or []) or actual_model in (provider.disabled_models or []):
                continue
            key = f"{provider.name}||{wanted}"
            if not force and (
                health_state.is_circuit_open(key)
                or health_state.health_status.get(key, {}).get("status") in {"deleted", "fail", "error"}
            ):
                continue
            candidates.append((provider, actual_model, wanted))

    if not candidates:
        logger.error("\033[91m✗ 无可用的模型候选: %s\033[0m", requested_model)
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}")

    # 按优先级排序：先按失败次数升序（失败少的优先），再按模型大小降序（大的优先）
    candidates.sort(key=lambda c: (health_state.get_fail_count(f"{c[0].name}||{c[2]}"), -health_state.get_model_size(f"{c[0].name}||{c[2]}")))
    
    logger.info("\033[93m▶ 路由请求: %s (%d 个候选)\033[0m", requested_model, len(candidates))

    last_error = "上游无响应"
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, verify=False) as client:
        for provider, actual_model, requested in candidates:
            upstream_body = dict(body)
            upstream_body["model"] = normalize_model_name(actual_model)
            # 若是流式：默认请求上游返回 usage（部分上游需显式要求 stream_options.include_usage）
            # 客户端已带 stream_options 则透传，不再覆盖
            if upstream_body.get("stream") and not isinstance(upstream_body.get("stream_options"), dict):
                upstream_body["stream_options"] = {"include_usage": True}
            url = provider.base_url.rstrip("/") + "/chat/completions"
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            }
            try:
                if upstream_body.get("stream"):
                    # 流式响应不能依赖外层 AsyncClient 上下文，响应返回后外层会立即关闭。
                    # 推理模型思考阶段可能长时间无字节输出，read 超时放宽到 600s 避免断流
                    stream_client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=15.0), verify=False)
                    upstream_request = client.build_request(
                        "POST", url, json=upstream_body, headers=headers
                    )
                    upstream = await stream_client.send(upstream_request, stream=True)
                    if upstream.status_code >= 400:
                        last_error = f"{provider.name}: HTTP {upstream.status_code}"
                        logger.warning("\033[91m✗ 失败: %s · %s (HTTP %d)\033[0m", provider.name, actual_model, upstream.status_code)
                        await upstream.aclose()
                        await stream_client.aclose()
                        # 记录失败并尝试下一候选
                        health_state.record_fail(f"{provider.name}||{requested}")
                        if upstream.status_code in RATE_LIMIT_STATUS_CODES:
                            if health_state.check_rate_limit(provider.name):
                                logger.info("\033[93m⏸ %s 处于限速冷却，跳过\033[0m", provider.name)
                        continue

                    async def stream_response():
                        prefix = f"🍀 {provider.name} - {actual_model}🌿🎋"
                        reasoning_open = False
                        _stream_ok = False  # 标记流是否完整结束
                        usage_obj = None     # 上游返回的 usage 对象（部分上游只在 [DONE] 前带）

                        try:
                            # 前缀作为流的第一个事件发出（独立首包），始终顶格在开头
                            first_evt = {
                                "choices": [{
                                    "delta": {"role": "assistant", "content": prefix + "\n\n", "reasoning_content": None},
                                    "index": 0,
                                    "finish_reason": None,
                                }],
                                "model": f"{provider.name} · {actual_model}",
                            }
                            yield ("data: " + json.dumps(first_evt, ensure_ascii=False) + "\n\n").encode("utf-8")
                            async for line in upstream.aiter_lines():
                                if not line:
                                    continue
                                if not line.startswith("data: "):
                                    yield restore_hermes_text(line + "\n").encode("utf-8")
                                    continue
                                data_str = line[6:]
                                if data_str.strip() == "[DONE]":
                                    _stream_ok = True  # 流完整结束，标记成功（在 finally 中记 success）
                                    if reasoning_open:
                                        yield "data: " + json.dumps({"choices": [{"delta": {"content": "</think> "}, "index": 0}]}, ensure_ascii=False) + "\n\n"
                                        reasoning_open = False
                                    yield "data: [DONE]\n\n"
                                    break
                                try:
                                    obj = json.loads(data_str)
                                    if "model" in obj and isinstance(obj["model"], str):
                                        obj["model"] = f"{provider.name} · {actual_model}"
                                    # 捕获上游 usage（部分上游只在最后一个事件带，这里一直更新为最新值）
                                    if isinstance(obj.get("usage"), dict):
                                        usage_obj = obj["usage"]
                                    choices = obj.get("choices") or []
                                    if choices:
                                        delta = choices[0].get("delta") or {}
                                        rc = delta.pop("reasoning_content", None)
                                        # 兼容字段名 reasoning（Aion 等上游用 reasoning 而非 reasoning_content）
                                        if rc is None and "reasoning" in delta:
                                            rc = delta.pop("reasoning", None)
                                        # 兼容 reasoning_details 数组（Kilo Code/nemotron 等上游）。
                                        # 无论 rc 是否已取，都 pop 掉防止透传给客户端；仅在 rc 为空时累积。
                                        rd = delta.pop("reasoning_details", None)
                                        if rc is None and rd is not None:
                                            if isinstance(rd, list):
                                                rc = "".join(
                                                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                                                    for item in rd
                                                )
                                            elif isinstance(rd, str):
                                                rc = rd
                                        if rc is not None:
                                            # 思考增量：首包加 <think> 标记，后续直接裸拼（不重复加标签）
                                            if not reasoning_open:
                                                reasoning_open = True
                                                rc = "<think> " + rc
                                            delta["content"] = rc
                                        elif reasoning_open and delta.get("content") is not None:
                                            # 正文开始：闭合思考块
                                            reasoning_open = False
                                            delta["content"] = "</think> " + (delta.get("content") or "")
                                        elif reasoning_open:
                                            # 思考中但本包无内容：忽略
                                            pass
                                    out = json.dumps(obj, ensure_ascii=False)
                                    out = restore_hermes_text(out)
                                    yield ("data: " + out + "\n\n").encode("utf-8")
                                except json.JSONDecodeError:
                                    yield restore_hermes_text(line + "\n").encode("utf-8")
                                except Exception as e:
                                    # 单个事件处理失败不应中断整个流：透传原始行
                                    logger.warning("\033[93m⚠ 流式事件处理异常: %s\033[0m", e)
                                    yield restore_hermes_text(line + "\n").encode("utf-8")
                        finally:
                            await upstream.aclose()
                            await stream_client.aclose()
                            if _stream_ok:
                                health_state.record_success(f"{provider.name}||{requested}")
                                logger.info("\033[32m✓ 成功: %s · %s\033[0m", provider.name, actual_model)
                                # 记录流式用量：上游 usage 可能在最后一个 chunk 里，不限 stream_options.include_usage
                                if usage_obj:
                                    try:
                                        await append_usage(
                                            provider=provider.name,
                                            model=requested,
                                            prompt_tokens=usage_obj.get("prompt_tokens", 0) or 0,
                                            completion_tokens=usage_obj.get("completion_tokens", 0) or 0,
                                            total_tokens=usage_obj.get("total_tokens", 0) or 0,
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to record stream usage: {e}")
                            else:
                                health_state.record_fail(f"{provider.name}||{requested}")
                                logger.warning("\033[91m✗ 流中断: %s · %s\033[0m", provider.name, actual_model)

                    return StreamingResponse(
                        stream_response(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )

                response = await client.post(url, json=upstream_body, headers=headers)
                if response.status_code < 400:
                    result = merge_reasoning(response.json())
                    if isinstance(result, dict) and result.get("error"):
                        error_detail = result["error"]
                        last_error = f"{provider.name}: {error_detail}"
                        logger.warning("\033[91m✗ 失败: %s · %s (响应包含 error)\033[0m", provider.name, actual_model)
                        health_state.record_fail(f"{provider.name}||{requested}")
                        continue
                    health_state.record_success(f"{provider.name}||{requested}")
                    logger.info("\033[32m✓ 成功: %s · %s (HTTP %d)\033[0m", provider.name, actual_model, response.status_code)
                    result.setdefault("model", f"{provider.name} · {actual_model}")
                    # 记录用量
                    try:
                        usage = result.get("usage", {})
                        if usage:
                            await append_usage(
                                provider=provider.name,
                                model=requested,
                                prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                                completion_tokens=usage.get("completion_tokens", 0) or 0,
                                total_tokens=usage.get("total_tokens", 0) or 0,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to record usage: {e}")
                    return JSONResponse(result, status_code=response.status_code)
                last_error = f"{provider.name}: HTTP {response.status_code}"
                logger.warning("\033[91m✗ 失败: %s · %s (HTTP %d)\033[0m", provider.name, actual_model, response.status_code)
                # 限流(429/503/403)才记录失败并尝试下一候选；
                # 其他错误（400/401/404/500 等）说明请求本身或上游配置有问题，
                # 换候选也没用，直接把上游错误透传给客户端，避免误判“候选不可用”
                if response.status_code in RATE_LIMIT_STATUS_CODES:
                    health_state.record_fail(f"{provider.name}||{requested}")
                    if health_state.check_rate_limit(provider.name):
                        logger.info("\033[93m⏸ %s 处于限速冷却，跳过\033[0m", provider.name)
                    continue
                # 非限流错误：记录失败（不参与下一候选），直接透传上游错误
                health_state.record_fail(f"{provider.name}||{requested}")
                try:
                    detail = response.json().get("error", {}).get("message") or response.text[:300]
                except Exception:
                    detail = response.text[:300]
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"上游错误 ({provider.name}): {detail}",
                )
            except httpx.HTTPError as exc:
                last_error = f"{provider.name}: {exc}"
                logger.warning("\033[91m✗ 失败: %s · %s (%s)\033[0m", provider.name, actual_model, exc)
                health_state.record_fail(f"{provider.name}||{requested}")

    raise HTTPException(503, f"所有路由候选均不可用: {last_error}")


@router.get("/models", dependencies=[Depends(verify_client)])
async def proxy_models():
    """返回可用模型列表"""
    from database.models import Provider
    from database.engine import db
    from services.meta_service import ModelMetaService
    from services.health_service import health_state
    
    models_list = []
    meta = ModelMetaService()
    
    # 路由组
    for router_name in ROUTERS:
        models_list.append({
            "id": router_name,
            "object": "model",
            "owned_by": "FlowGate",
            "available": True,
        })
    
    # 数据库中的提供商
    async with db.SessionLocal() as session:
        providers = await session.execute(select(Provider))
        for p in providers.scalars().all():
            disabled = set(p.disabled_models or [])
            for m in p.models or []:
                if m in disabled:
                    continue
                k = f"{p.name}||{m}"
                st = health_state.health_status.get(k, {}).get("status")
                if st == "deleted":
                    continue
                available = st in (None, "unknown", "ok")
                ctx_len = meta.context_limits.get(m) or meta.model_descriptions.get(m, {}).get("ctx") or 32768
                models_list.append({
                    "id": f"{p.name}-{m}",
                    "object": "model",
                    "owned_by": p.name,
                    "available": available,
                    "context_length": ctx_len,
                    "max_position_embeddings": ctx_len,
                    "max_model_len": ctx_len,
                })
    
    return {"object": "list", "data": models_list}
