"""服务层：精确请求缓存。

只缓存「确定性调用」：非流式、不带 tools、且 temperature 显式 <= 0.2。
默认（temperature 未给）不缓存——模型本来就会随机输出，缓存会让用户
以为「模型卡住了」。带 tools 的请求也不缓存：工具调用结果里常带时间戳、
随机 id，重放会出错。

命中直接返回上次的 JSON，不写 usage_records（没有真的消耗上游额度）。
"""
import hashlib
import json
import threading
import time
from collections import OrderedDict

MAX_ENTRIES = 256

_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "evictions": 0}


def make_key(body: dict, model: str) -> str:
    """只把影响输出的字段纳入 key，避免无关字段（如 user）降低命中率。"""
    payload = {
        "model": model,
        "messages": body.get("messages"),
        "max_tokens": body.get("max_tokens"),
        "max_completion_tokens": body.get("max_completion_tokens"),
        "temperature": body.get("temperature"),
        "top_p": body.get("top_p"),
        "stop": body.get("stop"),
        "seed": body.get("seed"),
        "response_format": body.get("response_format"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cacheable(body: dict) -> bool:
    """判断这次请求是否允许缓存。"""
    if body.get("stream"):
        return False
    if body.get("tools") or body.get("functions"):
        return False
    try:
        temperature = float(body.get("temperature"))
    except (TypeError, ValueError):
        return False
    return 0.0 <= temperature <= 0.2


def get(key: str, ttl: int) -> dict | None:
    if ttl <= 0:
        return None
    now = time.time()
    with _lock:
        item = _cache.get(key)
        if item is None:
            _stats["misses"] += 1
            return None
        ts, value = item
        if now - ts > ttl:
            _cache.pop(key, None)
            _stats["misses"] += 1
            return None
        _cache.move_to_end(key)
        _stats["hits"] += 1
        return json.loads(json.dumps(value))  # 深拷贝，调用方改了不会污染缓存


def put(key: str, value: dict) -> None:
    with _lock:
        _cache[key] = (time.time(), json.loads(json.dumps(value)))
        _cache.move_to_end(key)
        while len(_cache) > MAX_ENTRIES:
            _cache.popitem(last=False)
            _stats["evictions"] += 1


def stats() -> dict:
    with _lock:
        total = _stats["hits"] + _stats["misses"]
        return {
            **_stats,
            "entries": len(_cache),
            "hit_rate": round(_stats["hits"] / total * 100, 1) if total else 0.0,
        }


def clear() -> int:
    with _lock:
        n = len(_cache)
        _cache.clear()
    return n
