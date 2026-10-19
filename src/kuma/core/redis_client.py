"""Redis 基座：复用 orio-search-redis（DB5），**所有 key 带 `kuma:` 前缀且强制 TTL**。

该容器有 256MiB cgroup 硬限且 `maxmemory=0/noeviction`（Task-Server.md §4.1）——
kuma 写任何无 TTL 的 key 都可能把邻居容器撑爆，故这里不提供无 TTL 的写入口。
"""

from __future__ import annotations

from contextlib import suppress

from redis.asyncio import Redis, from_url

from .config import get_settings
from .logging import get_logger

log = get_logger("redis")

_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        s = get_settings()
        _client = from_url(
            s.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=30,
        )
    return _client


def kkey(*parts: str) -> str:
    """命名空间：kuma:<域>:<标识>。"""
    s = get_settings()
    return s.redis_prefix + ":".join(p for p in parts if p)


async def set_ttl(key: str, value: str, ttl: int) -> None:
    """唯一写入口：必须给 TTL。"""
    if ttl <= 0:
        raise ValueError("kuma 的 Redis key 必须带 TTL（§4.1 复用约束）")
    await get_redis().set(key, value, ex=ttl)


async def healthcheck() -> dict[str, str]:
    try:
        await get_redis().ping()
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": type(exc).__name__}


async def acquire_lock(name: str, ttl: int = 120) -> str | None:
    """worker 任务锁（§12-8：禁止任务重叠）。返回 token，释放用 release_lock。"""
    token = kkey("lock", name)
    ok = await get_redis().set(token, "1", nx=True, ex=ttl)
    return token if ok else None


async def release_lock(token: str) -> None:
    # 释放失败不致命：锁有 TTL，最多让下个周期早跑一次
    with suppress(Exception):
        await get_redis().delete(token)


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
