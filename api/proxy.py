"""代理接口路由（OpenAI 兼容）"""
import hmac
import json
import time
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Request, HTTPException, Depends, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
import httpx

from config.settings import settings
from services.health_service import health_state, RATE_LIMIT_STATUS_CODES
from services.usage_service import append_usage
from services.upstream_client import get_shared_client
from services import cache_service
from services.observability import new_trace_id, record_request
from services.virtual_key_service import add_tokens, check_limits, note_request, resolve as resolve_vkey

logger = logging.getLogger("flowgate.proxy")
UPSTREAM_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
# 流式请求：推理模型思考阶段可能长时间无字节输出，read 超时放宽到 600 秒避免断流
STREAM_TIMEOUT = httpx.Timeout(600.0, connect=15.0)


def note_rate_limit(provider_name: str, status_code: int, headers=None) -> None:
    """遇到限流状态码时记录 provider 级冷却（原来 record_rate_limit 从未被调用）。"""
    if status_code not in (429, 503) and status_code < 500:
        return
    retry_after = None
    if headers:
        raw = headers.get("retry-after")
        if raw:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                pass
    health_state.record_rate_limit(provider_name, retry_after)

security = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/v1")


async def verify_client(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """客户端鉴权：主 Key 或任一启用的虚拟 Key。

    主 Key 用 compare_digest 而不是 != ：字符串比较是短路的，逐字符比到不同
    就返回，理论上可以用响应时间差猜 Key（时序侧信道）。
    虚拟 Key 走数据库查表，各自受 RPM/TPM 限制；鉴权结果写到 request.state，
    转发层据此把用量归属到具体 Key。
    """
    expected = settings.load().local_api_key or ""
    provided = credentials.credentials if credentials else ""

    if expected and provided and hmac.compare_digest(provided, expected):
        request.state.api_key_name = "主 Key"
        request.state.vkey_id = None
        request.state.vkey = None
        return "master"

    vk = await resolve_vkey(provided)
    if vk is not None:
        allowed, reason = check_limits(vk["id"], vk["rpm_limit"], vk["tpm_limit"])
        if not allowed:
            raise HTTPException(status_code=429, detail=reason, headers={"Retry-After": "60"})
        request.state.api_key_name = vk["name"]
        request.state.vkey_id = vk["id"]
        request.state.vkey = vk
        note_request(vk["id"], 0)
        return vk["name"]

    raise HTTPException(status_code=401, detail="Invalid or missing API Key")


# Hermes 工具名压缩 / 还原
HERMES_MAP = [
    ("mcp_hermes_studio_use_hermes_studio_use_", "mcp_hermes_studio_use_hermes_studio_use_"),
    ("mcp_hermes_studio_devices_hermes_studio_lan_", "mcp_hermes_studio_devices_hermes_studio_lan_"),
    ("mcp_hermes_studio_api_hermes_studio_api_", "mcp_hermes_studio_api_hermes_studio_api_"),
]


def compress_hermes(obj: dict) -> dict:
    # HERMES_MAP 当前 long==short，实际无压缩效果；短路以免每次请求做两轮 JSON 序列化。
    return obj


def restore_hermes_text(text: str) -> str:
    return text


def merge_reasoning(obj: dict) -> dict:
    """合并 reasoning_content"""
    if not isinstance(obj, dict):
        return obj
    choices = obj.get("choices")
    if not choices or not isinstance(choices, list):
        return obj
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        target = choice.get("delta") or choice.get("message")
        if not target or not isinstance(target, dict):
            continue
        rc = target.pop("reasoning_content", None)
        if not isinstance(rc, str) or not rc:
            continue
        c = target.get("content")
        if isinstance(c, str) and c:
            target["content"] = rc + c
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


def ensure_lang_reply(body: dict, mode: str = "auto") -> dict:
    """注入回复语言提示。

    mode:
      - "auto"（默认）：按用户消息语种注入中文/英文提示
      - "off"：完全不注入。JSON 输出、代码生成、工具调用这类任务里，
        额外塞一段「请始终用简体中文回答」是纯干扰，需要时可在设置里关掉。
    """
    if str(mode or "auto").lower() == "off":
        return body
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body
    lang = detect_user_lang(msgs)
    hint = LANG_HINTS[lang]
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        c = first.get("content")
        if isinstance(c, str) and "请始终使用简体中文" not in c and "always respond to the user in English" not in c:
            first["content"] = c.rstrip() + hint
        return body
    sys_text = ("请使用简体中文回答。" + hint) if lang == "zh" else ("Please respond in English. " + hint)
    msgs.insert(0, {"role": "system", "content": sys_text})
    return body


@asynccontextmanager
async def _upstream_client(verify_ssl: bool):
    """给出一个上游 client：优先复用进程级连接池，没有则临时新建。

    产出 (client, shared)：shared=True 表示这是共享池，调用方**不要关**它
    （流式响应在请求函数返回后还要继续读，关早了会断流）。
    """
    shared = get_shared_client(verify_ssl)
    if shared is not None:
        yield shared, True
        return
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, verify=verify_ssl)
    try:
        yield client, False
    finally:
        await client.aclose()


async def normalize_router_name(name: str) -> str:
    """规范化路由组名称，支持英文别名（直接从数据库查询）

    两级匹配顺序有意义：先按组名，再按别名。原来一条 SQL 用 OR 把两种匹配
    混在一起、还用了 scalar_one_or_none()，只要别名重复（或别名撞上别的组名）
    就抛 MultipleResultsFound，导致这个模型名的所有请求 500。
    """
    if not isinstance(name, str) or not name.strip():
        return name
    from database.engine import db
    from database.models import RouterGroup
    async with db.SessionLocal() as session:
        # 组名有唯一约束，优先精确命中，避免别名撞名时取错组
        exact = await session.execute(
            select(RouterGroup).where(RouterGroup.name == name)
        )
        row = exact.scalars().first()
        if row is None:
            # 别名没有唯一约束（老数据可能重复），first() 兜底不抛异常
            alias_hit = await session.execute(
                select(RouterGroup)
                .where(RouterGroup.alias == name)
                .order_by(RouterGroup.id)
            )
            row = alias_hit.scalars().first()
    return row.name if row else name


@router.get("/routers/aliases")
async def get_router_aliases():
    """返回路由组别名映射 {alias: chinese_name}，供客户端使用"""
    from database.engine import db
    from database.models import RouterGroup
    async with db.SessionLocal() as session:
        result = await session.execute(select(RouterGroup))
        rows = result.scalars().all()
    aliases = {r.alias: r.name for r in rows if r.alias}
    return {"object": "list", "data": aliases}


def _content_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for seg in content:
            if isinstance(seg, dict):
                text = seg.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return 0


def _upstream_error_text(resp, limit: int = 300) -> str:
    """从上游错误响应里提取一句能看懂的原文。

    上游格式并不统一：
      - OpenAI 风格 {"error": {"message": "..."}}
      - Google 原生 [{"error": {"code": 400, "message": "..."}}]（顶层是数组）
      - 网关/中转直接吐纯文本
    以前 4xx/5xx 只保留状态码，400 的真实原因全部丢失，客户端和日志里都只剩
    「HTTP 400」，排查时无从下手。
    """
    try:
        text = (resp.text or "").strip()
    except Exception:
        return ""
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        return text.replace("\n", " ")[:limit]
    if isinstance(data, list) and data:
        data = data[0]
    msg = ""
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or err.get("type") or ""
        elif isinstance(err, str):
            msg = err
    return str(msg or text).replace("\n", " ")[:limit]


def estimate_request_tokens(body: dict) -> int:
    """粗估这次请求要占多少 token（输入 + max_tokens）。

    只用于「上下文窗口装不下就跳过这个模型」的过滤，不要求精确。
    中文约 1.5 字/token、英文约 4 字符/token，2.5 是折中值：宁可略高估
    （少试一个边缘模型），也不要投进去换一个 400。
    """
    chars = 0
    msgs = body.get("messages")
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                chars += _content_chars(m.get("content"))
    chars += _content_chars(body.get("prompt"))
    msg_count = len(msgs) if isinstance(msgs, list) else 0
    est_in = int(chars / 2.5) + 8 * msg_count
    try:
        est_out = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
    except (TypeError, ValueError):
        est_out = 0
    return est_in + max(0, est_out)


async def forward_chat(
    body: dict,
    requested_model: str,
    merge_reasoning_enabled: bool,
    force: bool = False,
    trace_id: str | None = None,
    endpoint: str = "/v1/chat/completions",
    api_key_name: str = "",
    vkey_id: int | None = None,
) -> Response:
    """候选筛选 + 上游转发 + 失败换下一家。

    从 proxy_chat 抽出来的原因：多协议入口（/v1/messages、Gemini）把请求
    转成 OpenAI 格式后可以直接复用这一整套逻辑，不用把 fallback 再写一遍。
    """
    cfg = settings.load()
    trace_id = trace_id or new_trace_id()
    started = time.time()
    attempts = 0
    last_error = "上游无响应"

    # ---- 请求缓存：只对确定性调用生效，命中不消耗上游额度 ----
    cache_key = None
    if bool(getattr(cfg, "cache_enabled", True)) and cache_service.cacheable(body):
        cache_key = cache_service.make_key(body, requested_model)
        hit = cache_service.get(cache_key, int(getattr(cfg, "cache_ttl", 300) or 0))
        if hit is not None:
            payload = dict(hit)
            meta_info = payload.pop("_fg_meta", {}) or {}
            logger.info("\033[36m✓ 缓存命中: %s\033[0m", requested_model)
            usage = payload.get("usage") or {}
            await record_request(
                trace_id=trace_id, requested_model=requested_model, endpoint=endpoint,
                provider=meta_info.get("provider"), upstream_model=meta_info.get("model"),
                stream=False, ok=True, status_code=200, attempts=0,
                latency_ms=int((time.time() - started) * 1000),
                prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                completion_tokens=usage.get("completion_tokens", 0) or 0,
                cached=True, api_key_name=api_key_name,
            )
            return JSONResponse(payload)

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
        wanted_models = router.models if router and isinstance(router.models, list) else [requested_model]
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
                # 命中前缀但模型不属于这家：继续看后面的 provider。
                # 以前 break 写在 if 内、不属于前缀命中就跳出，一旦提供商名字互为前缀
                # （如 "ab" 与 "ab-2"）就会直接放弃后续所有 provider，导致 503。
                continue

    est_tokens = estimate_request_tokens(body)
    candidates = []
    # 逐个候选记录「为什么被跳过」。不记录的话，「无可用的模型候选」只能靠猜，
    # 以前一律归因于 token 超限，真实原因可能是熔断、限流冷却，甚至模型根本没匹配上。
    matched = 0
    skip_ctx: list[str] = []
    skip_unhealthy = 0
    skip_cooldown = 0
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
            matched += 1
            # 上下文窗口过滤：长请求不打给小窗口模型，否则路由成功、上游直接 400
            ctx = meta.context_limits.get(actual_model) or meta.context_limits.get(wanted)
            if ctx and est_tokens > ctx * 0.95:
                skip_ctx.append(f"{provider.name}·{actual_model} 上限 {ctx}")
                logger.info(
                    "\033[93m↷ 跳过 %s · %s：请求约 %d tokens 超出上下文 %d\033[0m",
                    provider.name, actual_model, est_tokens, ctx,
                )
                continue
            key = f"{provider.name}||{wanted}"
            if not force and (
                health_state.is_circuit_open(key)
                or health_state.health_status.get(key, {}).get("status") in {"deleted", "fail", "error"}
            ):
                skip_unhealthy += 1
                continue
            if not force and health_state.check_rate_limit(provider.name):
                skip_cooldown += 1
                continue
            candidates.append((provider, actual_model, wanted))

    if not candidates:
        if matched == 0:
            reason = f"没有任何服务商提供该模型（请求约 {est_tokens} tokens）"
        elif skip_ctx and not skip_unhealthy and not skip_cooldown:
            reason = "请求约 %d tokens 超出全部 %d 个候选的上下文（%s）" % (
                est_tokens, len(skip_ctx), "、".join(skip_ctx[:3]),
            )
        else:
            parts = []
            if skip_unhealthy:
                parts.append(f"{skip_unhealthy} 个熔断/上轮失败")
            if skip_cooldown:
                parts.append(f"{skip_cooldown} 个限流冷却")
            if skip_ctx:
                parts.append(f"{len(skip_ctx)} 个上下文不足")
            reason = "全部候选被跳过（%s，请求约 %d tokens）" % ("、".join(parts), est_tokens)
        logger.error("\033[91m✗ 无可用的模型候选: %s｜%s\033[0m", requested_model, reason)
        await record_request(
            trace_id=trace_id, requested_model=requested_model, endpoint=endpoint,
            stream=bool(body.get("stream")), ok=False, status_code=503, attempts=0,
            latency_ms=int((time.time() - started) * 1000), api_key_name=api_key_name,
            error=f"无可用的模型候选：{reason}",
        )
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}｜{reason}")

    # 按质量分与延迟择优：质量分高、延迟低、失败少、模型大的优先
    # 无历史数据时质量分默认 1.0、延迟用大数占位，退化为按失败次数与大小排序
    def _quality_key(c):
        k = f"{c[0].name}||{c[2]}"
        quality = health_state.get_quality_score(k)
        latency = health_state.get_avg_latency(k)
        return (
            -quality,
            latency if latency is not None else float("inf"),
            health_state.get_fail_count(k),
            -health_state.get_model_size(k),
        )

    if str(getattr(cfg, "routing_strategy", "quality") or "quality").lower() == "cost":
        from services.cost_service import _price_maps
        exact_prices, lowest_prices = await _price_maps()

        def _cost_key(c):
            price = exact_prices.get((c[0].name, c[1])) or lowest_prices.get(c[1])
            cost = ((price[0] or 0.0) + (price[1] or 0.0)) if price else float("inf")
            return (cost,) + _quality_key(c)

        candidates.sort(key=_cost_key)
    else:
        candidates.sort(key=_quality_key)

    logger.info("\033[93m▶ 路由请求: %s (%d 个候选)\033[0m", requested_model, len(candidates))

    verify_ssl = bool(cfg.verify_ssl)
    # 复用进程级连接池：同一家上游的第二个请求可以直接走已有 TLS 连接，
    # TTFT 少 100~300ms（原先每个请求都新建 client，握手开销全算在首字延迟里）
    async with _upstream_client(verify_ssl) as (client, shared_conn):
        for provider, actual_model, requested in candidates:
            attempts += 1
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
                    # 流式响应在请求函数返回后还要继续读字节，不能依赖请求级 client 的
                    # async with（会在返回 StreamingResponse 后立刻关闭）。
                    # 有共享池就用它；没有（测试环境 / 刚改过 verify_ssl）才临时建一个。
                    stream_client = None if shared_conn else httpx.AsyncClient(
                        timeout=STREAM_TIMEOUT, verify=verify_ssl
                    )
                    sender = client if shared_conn else stream_client
                    try:
                        upstream_request = sender.build_request(
                            "POST", url, json=upstream_body, headers=headers,
                            timeout=STREAM_TIMEOUT,
                        )
                        upstream = await sender.send(upstream_request, stream=True)
                    except BaseException:
                        if stream_client is not None:
                            await stream_client.aclose()
                        raise
                    if upstream.status_code >= 400:
                        # 先把响应体读出来再关连接：上游的错误原文（限流原因、参数非法、
                        # 内容超长）只有这里有，以前读完只打进控制台，last_error 里仍只有
                        # 状态码，客户端和 request_logs 都看不到原因。
                        detail = ""
                        try:
                            await upstream.aread()
                            detail = _upstream_error_text(upstream)
                        except Exception:
                            detail = ""
                        last_error = f"{provider.name}: HTTP {upstream.status_code}"
                        if detail:
                            last_error += " — " + detail
                        logger.warning(
                            "\033[91m✗ 失败: %s · %s (HTTP %d) %s\033[0m",
                            provider.name, actual_model, upstream.status_code, detail[:200],
                        )
                        # 记录失败，并让 429/5xx 进冷却（以前只在非流式路径记，流式请求会一直撞同一家）
                        health_state.record_fail(f"{provider.name}||{requested}")
                        note_rate_limit(provider.name, upstream.status_code, upstream.headers)
                        await upstream.aclose()
                        if stream_client is not None:
                            await stream_client.aclose()
                        # 所有错误都尝试下一候选（包括限流和其他错误）
                        continue

                    async def stream_response():
                        prefix = f"🍀 {provider.name} - {actual_model}🌿🎋"
                        reasoning_open = False
                        _stream_ok = False  # 标记流是否完整结束
                        usage_obj = None     # 上游返回的 usage 对象（部分上游只在 [DONE] 前带）

                        try:
                            # 合并推理内容开启时，才在流开头显示上游模型前缀。
                            if merge_reasoning_enabled:
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
                                        yield "data: " + json.dumps({"choices": [{"delta": {"content": "</think>"}, "index": 0}]}, ensure_ascii=False) + "\n\n"
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
                                    if not isinstance(obj, dict):
                                        yield restore_hermes_text(line + "\n").encode("utf-8")
                                        continue
                                    choices = obj.get("choices")
                                    if (isinstance(choices, list) and choices
                                            and isinstance(choices[0], dict) and merge_reasoning_enabled):
                                        delta = choices[0].get("delta")
                                        if not isinstance(delta, dict):
                                            delta = {}
                                            choices[0]["delta"] = delta
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
                                        orig_content = delta.get("content")
                                        if isinstance(rc, str) and rc and isinstance(orig_content, str) and orig_content:
                                            # 思考增量：首包加 <think> 标记，后续直接裸拼（不重复加标签）
                                            if not reasoning_open:
                                                reasoning_open = True
                                                rc = "<think> " + rc
                                            # 保留同包中的正文 content（部分上游过渡包同时带 reasoning_content 和 content）
                                            # 过渡包：只在确有正文时合并 reasoning，避免空正文暴露 reasoning
                                            delta["content"] = rc + "\n\n🍀 " + orig_content
                                            reasoning_open = False
                                        elif reasoning_open and delta.get("content") is not None:
                                            # 正文开始：闭合思考块；如果后面有正文内容才换行，否则不变
                                            reasoning_open = False
                                            content = delta.get("content") or ""
                                            if content:
                                                delta["content"] = "</think>\r\n" + content
                                            else:
                                                delta["content"] = "</think>"
                                        elif reasoning_open:
                                            # 思考中但本包无内容：忽略
                                            pass
                                    out = json.dumps(obj, ensure_ascii=False)
                                    out = restore_hermes_text(out)
                                    yield ("data: " + out + "\n\n").encode("utf-8")
                                except json.JSONDecodeError:
                                    yield restore_hermes_text(line + "\n").encode("utf-8")
                                except (ValueError, TypeError, KeyError, AttributeError) as e:
                                    # 单个事件处理失败不应中断整个流：透传原始行
                                    logger.warning("\033[93m⚠ 流式事件处理异常: %s\033[0m", e)
                                    yield restore_hermes_text(line + "\n").encode("utf-8")
                        finally:
                            await upstream.aclose()
                            if stream_client is not None:
                                await stream_client.aclose()
                            usage_obj = usage_obj or {}
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
                                            upstream_model=actual_model,
                                        )
                                    except Exception as e:
                                        logger.warning(f"Failed to record stream usage: {e}")
                            else:
                                health_state.record_fail(f"{provider.name}||{requested}")
                                logger.warning("\033[91m✗ 流中断: %s · %s\033[0m", provider.name, actual_model)
                            pt = usage_obj.get("prompt_tokens", 0) or 0
                            ct = usage_obj.get("completion_tokens", 0) or 0
                            await record_request(
                                trace_id=trace_id, requested_model=requested_model, endpoint=endpoint,
                                provider=provider.name, upstream_model=actual_model, stream=True,
                                ok=_stream_ok, status_code=200 if _stream_ok else None,
                                attempts=attempts,
                                latency_ms=int((time.time() - started) * 1000),
                                prompt_tokens=pt, completion_tokens=ct, api_key_name=api_key_name,
                                error=None if _stream_ok else "流未正常结束（客户端断开或上游截断）",
                            )
                            if vkey_id:
                                add_tokens(vkey_id, pt + ct)

                    return StreamingResponse(
                        stream_response(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                    )

                response = await client.post(url, json=upstream_body, headers=headers)
                try:
                    response_json = response.json()
                except (json.JSONDecodeError, ValueError):
                    # 响应不是有效的 JSON，记录后尝试下一个候选
                    _txt = (response.text or "").replace("\n", " ")[:200]
                    last_error = f"{provider.name}: 非JSON响应" + (f" — {_txt}" if _txt else "")
                    logger.warning("\033[91m✗ 失败: %s · %s (非JSON响应)\033[0m", provider.name, actual_model)
                    health_state.record_fail(f"{provider.name}||{requested}")
                    continue

                if not isinstance(response_json, dict):
                    # Google 原生错误体就是一个数组：[{"error":{"code":400,"message":...}}]
                    _txt = _upstream_error_text(response) or "非对象 JSON"
                    last_error = f"{provider.name}: 顶层不是对象 — {_txt}"
                    logger.warning("\033[91m✗ 失败: %s · %s (JSON顶层不是对象)\033[0m", provider.name, actual_model)
                    health_state.record_fail(f"{provider.name}||{requested}")
                    continue

                if response.status_code < 400:
                    result = merge_reasoning(response_json) if merge_reasoning_enabled else response_json
                    if isinstance(result, dict) and result.get("error"):
                        error_detail = result["error"]
                        last_error = f"{provider.name}: {error_detail}"
                        logger.warning("\033[91m✗ 失败: %s · %s (响应包含 error)\033[0m", provider.name, actual_model)
                        health_state.record_fail(f"{provider.name}||{requested}")
                        # 200 包 error 时也要换候选（原来这里直接 raise，注释却写「尝试下一候选」，
                        # 一家通道坏了整个请求就失败，完全不走 fallback）
                        err_text = str(error_detail).lower()
                        if any(k in err_text for k in (
                            "quota", "insufficient", "rate limit", "rate_limit",
                            "too many requests", "exceeded", "余额", "额度", "欠费",
                        )):
                            note_rate_limit(provider.name, 429, None)
                        continue
                    health_state.record_success(f"{provider.name}||{requested}")
                    logger.info("\033[32m✓ 成功: %s · %s (HTTP %d)\033[0m", provider.name, actual_model, response.status_code)
                    result.setdefault("model", f"{provider.name} · {actual_model}")
                    usage = result.get("usage") or {}
                    pt = usage.get("prompt_tokens", 0) or 0
                    ct = usage.get("completion_tokens", 0) or 0
                    # 记录用量
                    try:
                        if usage:
                            await append_usage(
                                provider=provider.name,
                                model=requested,
                                prompt_tokens=pt,
                                completion_tokens=ct,
                                total_tokens=usage.get("total_tokens", 0) or 0,
                                upstream_model=actual_model,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to record usage: {e}")
                    if cache_key is not None:
                        cached_value = dict(result)
                        cached_value["_fg_meta"] = {"provider": provider.name, "model": actual_model}
                        cache_service.put(cache_key, cached_value)
                    await record_request(
                        trace_id=trace_id, requested_model=requested_model, endpoint=endpoint,
                        provider=provider.name, upstream_model=actual_model, stream=False,
                        ok=True, status_code=response.status_code, attempts=attempts,
                        latency_ms=int((time.time() - started) * 1000),
                        prompt_tokens=pt, completion_tokens=ct, api_key_name=api_key_name,
                    )
                    if vkey_id:
                        add_tokens(vkey_id, pt + ct)
                    return JSONResponse(result, status_code=response.status_code)
                _txt = _upstream_error_text(response)
                last_error = f"{provider.name}: HTTP {response.status_code}" + (f" — {_txt}" if _txt else "")
                logger.warning(
                    "\033[91m✗ 失败: %s · %s (HTTP %d) %s\033[0m",
                    provider.name, actual_model, response.status_code, _txt[:200],
                )
                # 记录失败并尝试下一候选（含400等继续尝试下一候选）
                health_state.record_fail(f"{provider.name}||{requested}")
                note_rate_limit(provider.name, response.status_code, response.headers)
                if health_state.check_rate_limit(provider.name):
                    logger.info("\033[93m⏸ %s 处于限速冷却\033[0m", provider.name)
                continue
            except httpx.HTTPError as exc:
                last_error = f"{provider.name}: {exc}"
                logger.warning("\033[91m✗ 失败: %s · %s (%s)\033[0m", provider.name, actual_model, exc)
                health_state.record_fail(f"{provider.name}||{requested}")

    await record_request(
        trace_id=trace_id, requested_model=requested_model, endpoint=endpoint,
        stream=bool(body.get("stream")), ok=False, status_code=503, attempts=attempts,
        latency_ms=int((time.time() - started) * 1000), api_key_name=api_key_name,
        error=last_error,
    )
    raise HTTPException(503, f"所有路由候选均不可用: {last_error}")


@router.api_route("/chat/completions", methods=["POST"], dependencies=[Depends(verify_client)])
async def proxy_chat(request: Request, force: bool = False):
    """代理聊天补全请求（OpenAI 兼容入口）。"""
    # 每个请求固定一次配置，避免流式响应期间切换开关导致处理方式变化。
    cfg = settings.load()
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "请求体必须是有效的 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    body = compress_hermes(body)
    body = ensure_lang_reply(body, str(getattr(cfg, "lang_hint", "auto") or "auto"))
    requested_model = body.get("model")
    # 规范化路由组名称（支持英文别名，直接从数据库查询）
    requested_model = await normalize_router_name(requested_model)
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(400, "请求缺少 model")
    return await forward_chat(
        body, requested_model, bool(cfg.merge_reasoning), force=force,
        endpoint="/v1/chat/completions",
        api_key_name=getattr(request.state, "api_key_name", ""),
        vkey_id=getattr(request.state, "vkey_id", None),
    )



@router.get("/models", dependencies=[Depends(verify_client)])
async def proxy_models():
    """返回可用模型列表"""
    from database.models import Provider, RouterGroup
    from database.engine import db
    from services.meta_service import ModelMetaService
    from services.health_service import health_state
    
    models_list = []
    meta = ModelMetaService()
    
    # 数据库中的提供商
    async with db.SessionLocal() as session:
        # 路由组（从数据库读取，与 /chat/completions 一致，动态增删改实时生效）
        router_result = await session.execute(select(RouterGroup))
        for r in router_result.scalars().all():
            models_list.append({
                "id": r.name,
                "object": "model",
                "owned_by": "FlowGate",
                "available": True,
            })
        
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
