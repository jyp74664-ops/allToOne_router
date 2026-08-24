"""服务层：健康探测"""
import asyncio
import re
import time
import logging
from typing import Optional
from collections import deque

import httpx

from config.settings import settings

logger = logging.getLogger("flowgate.health")

# 常量
ONE_MILLION = 1_048_576
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_RECOVERY_SECONDS = 60
QUALITY_WINDOW = 20
RATE_LIMIT_STATUS_CODES = {429, 503, 403}
FAILED_MODEL_TTL = 120
UPSTREAM_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class HealthState:
    """健康状态内存管理器"""
    
    def __init__(self):
        self.health_status: dict = {}          # key -> {status, code, latency_ms, ...}
        self.model_quality: dict = {}          # key -> {ok, fail, error, latencies}
        self.circuit_breaker: dict = {}        # key -> {fails, open_until}
        self.failed_models: dict = {}          # key -> timestamp
        self.provider_api_status: dict = {}    # provider_name -> ok/error
        self.model_details: dict = {}          # model_id -> {context_length, ...}
        self._lock = asyncio.Lock()
        # 速率限制跟踪
        self.rate_limit_tracker: dict = {}     # provider_name -> {count, window_start}
        self.provider_rate_limited: dict = {}  # provider_name -> until_timestamp
        self.model_rate_limited_until: dict = {}  # key -> timestamp (429 冷却时间)
    
    def get_quality_score(self, key: str) -> float:
        """获取质量分 0~1"""
        q = self.model_quality.get(key)
        if not q:
            return 1.0
        total = q["ok"] + q["fail"] + q["error"]
        if total == 0:
            return 1.0
        return q["ok"] / total
    
    def get_avg_latency(self, key: str) -> Optional[float]:
        """获取平均延迟"""
        q = self.model_quality.get(key)
        if not q or not q["latencies"]:
            return None
        return sum(q["latencies"]) / len(q["latencies"])
    
    def get_fail_count(self, key: str) -> int:
        """获取失败次数（用于路由排序）"""
        cb = self.circuit_breaker.get(key)
        if not cb:
            return 0
        return cb.get("fails", 0)
    
    def get_model_size(self, key: str) -> float:
        """获取模型大小（用于路由排序，大的优先）"""
        # key 格式: "provider||model"
        parts = key.split("||")
        if len(parts) != 2:
            return 0
        model = parts[1]
        
        # 先从 model_details 获取（运行时更新）
        d = self.model_details.get(model, {})
        if d:
            size_str = d.get("size", "")
            if size_str:
                import re
                match = re.search(r'(\d+(?:\.\d+)?)', str(size_str))
                if match:
                    return float(match.group(1))
        
        # 回退到 ModelMetaService
        try:
            from services.meta_service import ModelMetaService
            meta = ModelMetaService()
            desc = meta.model_descriptions.get(model, {})
            size_str = desc.get("size", "")
            if size_str:
                import re
                match = re.search(r'(\d+(?:\.\d+)?)', str(size_str))
                if match:
                    return float(match.group(1))
        except Exception:
            pass
        
        return 0
    
    def clear_fail_count(self, key: str) -> bool:
        """清除指定模型的失败标记"""
        if key in self.circuit_breaker:
            del self.circuit_breaker[key]
            return True
        return False
    
    def clear_all_fail_counts(self) -> int:
        """清除所有失败标记"""
        count = len(self.circuit_breaker)
        self.circuit_breaker.clear()
        return count
    
    def get_test_count(self, key: str) -> int:
        """获取测试总次数"""
        q = self.model_quality.get(key)
        if not q:
            return 0
        return q["ok"] + q["fail"] + q["error"]
    
    def get_priority_score(self, key: str) -> tuple:
        """获取路由优先级分数（测试次数降序，失败次数升序）"""
        test_count = self.get_test_count(key)
        fail_count = self.get_fail_count(key)
        # 优先：测试次数多（有历史记录），其次：失败次数少
        return (-test_count, fail_count)
    
    def is_circuit_open(self, key: str) -> bool:
        """判断熔断器是否开启"""
        cb = self.circuit_breaker.get(key)
        if not cb:
            return False
        return bool(cb.get("open_until")) and time.time() < cb["open_until"]
    
    def record_fail(self, key: str) -> None:
        """记录一次失败"""
        self.failed_models[key] = time.time()
        cb = self.circuit_breaker.setdefault(key, {"fails": 0, "open_until": 0})
        cb["fails"] += 1
        if cb["fails"] >= CIRCUIT_FAIL_THRESHOLD:
            cb["open_until"] = time.time() + CIRCUIT_RECOVERY_SECONDS
            logger.warning("circuit opened: %s", key)
    
    def record_success(self, key: str) -> None:
        """记录一次成功"""
        self.failed_models.pop(key, None)
        cb = self.circuit_breaker.get(key)
        if cb:
            cb["fails"] = 0
            cb["open_until"] = 0
    
    def update_quality(self, key: str, info: dict) -> None:
        """更新模型质量滑动窗口"""
        q = self.model_quality.get(key)
        if q is None:
            q = {"ok": 0, "fail": 0, "error": 0, "latencies": deque(maxlen=QUALITY_WINDOW)}
            self.model_quality[key] = q
        st = info.get("status", "unknown")
        if st == "ok":
            q["ok"] += 1
            lat = info.get("latency_ms")
            if lat:
                q["latencies"].append(lat)
        elif st == "fail":
            q["fail"] += 1
        elif st == "error":
            q["error"] += 1
    
    def get_provider_status(self, provider_name: str) -> Optional[str]:
        """获取提供商状态 ok/error"""
        return self.provider_api_status.get(provider_name)
    
    def set_provider_status(self, provider_name: str, status: str) -> None:
        """设置提供商状态"""
        self.provider_api_status[provider_name] = status
    
    def check_rate_limit(self, provider_name: str) -> bool:
        """检查服务商是否处于限速状态"""
        limited_until = self.provider_rate_limited.get(provider_name, 0)
        if limited_until and time.time() < limited_until:
            return True
        return False
    
    def record_rate_limit(self, provider_name: str, retry_after: Optional[float] = None) -> None:
        """记录服务商被限速，设置重试时间"""
        if retry_after and retry_after > 0:
            wait_time = min(retry_after + 1, 65)  # 至少等到下一分钟
        else:
            wait_time = 60
        self.provider_rate_limited[provider_name] = time.time() + wait_time
    
    def is_model_rate_limited(self, key: str) -> bool:
        """检查模型是否在冷却期内"""
        until = self.model_rate_limited_until.get(key, 0)
        return time.time() < until
    
    def get_model_cooldown_remaining(self, key: str) -> int:
        """获取模型冷却剩余秒数"""
        until = self.model_rate_limited_until.get(key, 0)
        return max(0, int(until - time.time()))
    
    def record_model_rate_limit(self, key: str, retry_after: Optional[float] = None) -> None:
        """记录模型被限速，设置冷却时间"""
        if retry_after and retry_after > 0:
            wait_time = min(retry_after + 1, 65)
        else:
            wait_time = 30  # 默认冷却 30 秒
        self.model_rate_limited_until[key] = time.time() + wait_time
        logger.info("模型 %s 进入冷却 %.0f 秒", key, wait_time)


# 全局状态
health_state = HealthState()


def normalize_model_name(model: str) -> str:
    """去除常见前缀，使模型名适合发送到上游 API。
    例如 Google AI Studio 的 models/xxx 需要转为 xxx。"""
    return model.removeprefix("models/")


async def check_model(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    aliases: dict,
    max_retries: int = 2,
) -> dict:
    """探测单个模型的健康状态"""
    actual_model = normalize_model_name(aliases.get(model, model))
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": actual_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    last_error = None
    last_response = None
    
    for attempt in range(1, max_retries + 1):
        start = time.time()
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=UPSTREAM_TIMEOUT)
            latency = round((time.time() - start) * 1000)
            last_response = resp
            
            if resp.status_code == 200:
                usage = resp.json().get("usage", {})
                return {
                    "status": "ok",
                    "code": resp.status_code,
                    "latency_ms": latency,
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                }
            if resp.status_code == 429:
                # 解析 Retry-After 头
                retry_after = None
                ra_header = resp.headers.get("retry-after")
                if ra_header:
                    try:
                        retry_after = float(ra_header)
                    except ValueError:
                        pass
                # 返回限速状态，让调用方决定重试策略
                return {
                    "status": "rate_limited",
                    "code": resp.status_code,
                    "latency_ms": latency,
                    "detail": resp.text[:200],
                    "retry_after": retry_after,
                    "attempt": attempt,
                }
            if resp.status_code == 404:
                return {
                    "status": "deleted",
                    "code": resp.status_code,
                    "latency_ms": latency,
                    "detail": resp.text[:200],
                }
            # 其他错误状态
            return {
                "status": "fail",
                "code": resp.status_code,
                "latency_ms": latency,
                "detail": resp.text[:200],
            }
        except Exception as e:
            latency = round((time.time() - start) * 1000)
            last_error = str(e)
            if attempt < max_retries:
                logger.debug("检测模型 %s 失败 (尝试 %d/%d): %s", model, attempt, max_retries, last_error)
                await asyncio.sleep(1)
    
    # 所有重试都失败
    if last_response and last_response.status_code == 429:
        retry_after = last_response.headers.get("retry-after")
        try:
            retry_after = float(retry_after) if retry_after else None
        except (ValueError, TypeError):
            retry_after = None
        return {
            "status": "rate_limited",
            "code": last_response.status_code,
            "latency_ms": latency,
            "detail": last_response.text[:200],
            "retry_after": retry_after,
            "attempt": max_retries,
        }
    return {"status": "error", "latency_ms": latency, "detail": last_error}


async def fetch_models(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    free_only: bool,
    aliases: dict,
    context_limits: dict,
) -> list[str]:
    """拉取模型列表"""
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    url = base_url.rstrip("/") + "/models"
    max_retries = 2
    retry_delay = 1.0
    last_error = ""
    
    # 尝试两种认证方式：先无认证，再带认证
    auth_attempts = []
    if api_key:
        auth_attempts = [
            ({}, "no_auth"),
            ({"Authorization": f"Bearer {api_key}"}, "with_auth"),
        ]
    else:
        auth_attempts = [({}, "no_auth")]
    
    for headers, auth_type in auth_attempts:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.get(url, headers=headers or None, timeout=10)
                logger.info("fetch_models: %s attempt %d, status=%d, auth=%s", base_url, attempt, resp.status_code, auth_type)
                if resp.status_code == 200:
                    data = resp.json()
                    # 兼容不同提供商的响应格式：OpenAI 用 "data"，部分用 "models"
                    model_list = data.get("data") or data.get("models")
                    if isinstance(model_list, list):
                        pass  # 已是列表
                    elif isinstance(data, list):
                        model_list = data
                    else:
                        model_list = []
                    if not isinstance(model_list, list):
                        return []
                    seen: set[str] = set()
                    result: list[str] = []
                    for m in model_list:
                        if not isinstance(m, dict) or "id" not in m:
                            continue
                        raw_id = m["id"]
                        model_id = raw_id.removeprefix("models/")
                        if model_id in seen:
                            continue
                        seen.add(model_id)
                        # 过滤非聊天模型
                        lower = model_id.lower()
                        non_chat = ["image", "dall-e", "whisper", "tts", "audio", "text-embedding"]
                        if any(kw in lower for kw in non_chat):
                            continue
                        # 免费过滤
                        if free_only:
                            pricing = m.get("pricing", {})
                            try:
                                if float(pricing.get("prompt", 0)) != 0 or float(pricing.get("completion", 0)) != 0:
                                    continue
                            except (ValueError, TypeError):
                                pass
                        result.append(model_id)
                    return result
                last_error = f"HTTP {resp.status_code} ({auth_type})"
                logger.warning("fetch_models attempt %d/%d failed for %s: %s", attempt, max_retries, base_url, last_error)
            except Exception as e:
                last_error = str(e)
                logger.warning("fetch_models attempt %d/%d error for %s: %s", attempt, max_retries, base_url, e)
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
        # 如果无认证失败且有待用的带认证重试，继续尝试
        if auth_type == "no_auth" and api_key:
            logger.info("No-auth fetch failed for %s, retrying with auth", base_url)
            continue
        break
    
    logger.error("fetch_models failed after all attempts for %s: %s", base_url, last_error)
    return []


def get_context_length(model: str, aliases: dict, context_limits: dict, details: dict) -> int:
    """获取模型的上下文长度"""
    actual = aliases.get(model, model)
    ctx = context_limits.get(model) or context_limits.get(actual)
    if ctx:
        return ctx
    return details.get(actual, {}).get("context_length") or 32768


def is_1m_model(model: str, aliases: dict, context_limits: dict, details: dict) -> bool:
    """判断是否为百万级上下文模型"""
    ctx = get_context_length(model, aliases, context_limits, details)
    return bool(ctx) and ctx >= ONE_MILLION


def mask_key(key: str) -> str:
    """脱敏处理 API Key"""
    if not key or len(key) <= 12:
        return "****"
    return key[:6] + "****" + key[-4:]


async def run_full_check(on_progress=None) -> dict:
    """检查所有启用模型并更新健康状态。
    
    Args:
        on_progress: 可选的进度回调函数，接收 (provider, model, status, current, total) 参数
    """
    from sqlalchemy import select
    from database.engine import db
    from database.models import Provider
    from services.meta_service import ModelMetaService
    from config.settings import settings
    
    meta = ModelMetaService()
    results = {}
    async with db.SessionLocal() as session:
        providers = (await session.execute(select(Provider))).scalars().all()
    
    total = sum(len(p.models or []) + len(p.disabled_models or []) for p in providers)
    current = 0
    
    logger.info("\033[93m▶ 开始全量健康检测，共 %d 个服务商，%d 个模型\033[0m", len(providers), total)
    
    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, verify=False) as client:
        # 记录本次检测中已确认删除的模型，避免重复探测
        already_deleted: set[str] = set()
        for provider in providers:
            # 同时检查启用的模型和限额型模型（disabled_models 也要健康探测）
            models_to_check = [
                m for m in (provider.models or []) + (provider.disabled_models or [])
                if m not in (provider.disabled_models or []) or True  # 都检查
            ]
            models_to_check = list(dict.fromkeys(models_to_check))  # 去重
            logger.info("\033[94m  检测服务商: %s (%d 个模型)\033[0m", provider.name, len(models_to_check))
            
            for model in models_to_check:
                key = f"{provider.name}||{model}"
                
                # 检查是否在冷却期内（429 限速后跳过）
                if health_state.is_model_rate_limited(key):
                    remaining = health_state.get_model_cooldown_remaining(key)
                    logger.info("  [跳过] %s | %s (冷却中 %ds)", provider.name, model, remaining)
                    if on_progress:
                        on_progress(provider.name, model, "cooldown", current + 1, total)
                    continue
                
                # 跳过本次检测中已确认删除的模型（避免重复探测 404）
                if key in already_deleted:
                    logger.info("  [跳过] %s | %s (已标记 deleted)", provider.name, model)
                    if on_progress:
                        on_progress(provider.name, model, "deleted", current + 1, total)
                    current += 1
                    continue
                
                # 跳过上次检测已确认删除的模型（内存中持久保留，直到同步清除）
                prev_status = health_state.health_status.get(key, {}).get("status")
                if prev_status == "deleted":
                    logger.info("  [跳过] %s | %s (上一轮已 deleted)", provider.name, model)
                    if on_progress:
                        on_progress(provider.name, model, "deleted", current + 1, total)
                    current += 1
                    continue
                
                result = await check_model(
                    client, provider.base_url, provider.api_key, model, meta.aliases
                )
                results[key] = result
                health_state.health_status[key] = result
                health_state.update_quality(key, result)
                
                status = result.get("status", "unknown")
                
                # 如果是 429 限速，记录冷却时间
                if status == "rate_limited":
                    retry_after = result.get("retry_after")
                    health_state.record_model_rate_limit(key, retry_after)
                
                # 如果是 404 deleted，记录到跳过集合，避免重复探测
                if status == "deleted":
                    already_deleted.add(key)
                
                if status == "ok":
                    # 模型恢复，清除 deleted 标记
                    if prev_status == "deleted":
                        already_deleted.discard(key)
                        logger.info("  [恢复] %s | %s -> ok (取消 deleted 标记)", provider.name, model)
                    health_state.record_success(key)
                else:
                    health_state.record_fail(key)
                
                current += 1
                if on_progress:
                    on_progress(provider.name, model, status, current, total)
                logger.info("  [%d/%d] %s | %s -> %s", current, total, provider.name, model, status)
    
    ok_count = sum(1 for v in results.values() if v.get("status") == "ok")
    fail_count = len(results) - ok_count
    logger.info("\033[92m✓ 健康检测完成: %d 正常 / %d 异常\033[0m", ok_count, fail_count)
    
    # 记录检查时间
    settings._last_check_time = __import__('time').time()
    return results


def update_provider_status(provider_name: str) -> str:
    """根据模型检测结果更新提供商状态。"""
    statuses = [
        value.get("status")
        for key, value in health_state.health_status.items()
        if key.startswith(provider_name + "||")
    ]
    status = "ok" if any(item == "ok" for item in statuses) else "error"
    health_state.set_provider_status(provider_name, status)
    return status
