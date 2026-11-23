"""上游 httpx 客户端连接池。

以前每个代理请求都新建一个 httpx.AsyncClient，流式请求还要再建一个：
每次请求都要重做一遍 DNS + TCP + TLS 握手，TTFT 白加 100~300ms，
并发一高还会在系统里堆大量 TIME_WAIT。

现在由 app 的 lifespan 建一个进程级 client，转发路径复用它的连接池
（keep-alive 60 秒），请求结束只释放响应、不关连接。

注意：client 与事件循环绑定。测试里每个 TestClient 都是新的事件循环，
所以这里不做「进程级单例」的强制缓存，而是由调用方在使用前取一次；
没有共享 client（未启动完 / verify_ssl 被改过 / 测试环境）时，
调用方退回「每次新建」的老路径，行为不会因此坏掉。
"""
import logging

import httpx

logger = logging.getLogger("flowgate.upstream")

# 连接池上限：家用场景够用，同时防止异常时把本机文件句柄吃光
MAX_CONNECTIONS = 64
MAX_KEEPALIVE = 32
KEEPALIVE_EXPIRY = 60.0
# 默认读超时与代理层保持一致（流式请求会按请求单独放宽到 600 秒）
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

_client: httpx.AsyncClient | None = None
_verify_snapshot: bool | None = None
# 测试钩子：测试里把 proxy 的 httpx 换成了 MockTransport，共享池会绕开它
# 去连真实网络，所以测试期强制关闭复用（走「每次新建」路径）。
_force_disabled = False


def disable_shared_client() -> None:
    """强制不复用连接池（仅测试用）。"""
    global _force_disabled
    _force_disabled = True


def enable_shared_client() -> None:
    """恢复连接池复用（仅测试用）。"""
    global _force_disabled
    _force_disabled = False


def get_shared_client(verify_ssl: bool) -> httpx.AsyncClient | None:
    """取可复用的上游 client；没有或已失效时返回 None（调用方自行新建）。"""
    if _force_disabled:
        return None
    if _client is None or _client.is_closed:
        return None
    if _verify_snapshot != bool(verify_ssl):
        # verify_ssl 是运行时开关：改了之后旧连接不可信，交给调用方新建，
        # 下次重启（或再次 open）才会重建共享池。
        return None
    return _client


async def open_shared_client(verify_ssl: bool) -> httpx.AsyncClient:
    """创建（并替换）进程级上游 client。"""
    global _client, _verify_snapshot
    await close_shared_client()
    _client = httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        verify=verify_ssl,
        limits=httpx.Limits(
            max_connections=MAX_CONNECTIONS,
            max_keepalive_connections=MAX_KEEPALIVE,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        ),
    )
    _verify_snapshot = bool(verify_ssl)
    logger.info("上游连接池已建立（verify_ssl=%s，keepalive=%ss）", bool(verify_ssl), int(KEEPALIVE_EXPIRY))
    return _client


async def close_shared_client() -> None:
    """关闭进程级上游 client（幂等）。"""
    global _client, _verify_snapshot
    client, _client, _verify_snapshot = _client, None, None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception as exc:  # 关闭失败不影响退出流程
        logger.warning("关闭上游连接池失败: %s", exc)
