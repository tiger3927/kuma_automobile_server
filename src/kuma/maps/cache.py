"""OD 缓存（Redis，TTL 默认 1h）：同一起终点 + 交通方式 + 时段桶命中即返回。"""

from __future__ import annotations

import json

from ..core.config import get_settings
from ..core.redis_client import get_redis, kkey, set_ttl
from .base import RouteResult


def _key(od_key: str) -> str:
    return kkey("maps", "od", od_key)


async def get(od_key: str) -> RouteResult | None:
    raw = await get_redis().get(_key(od_key))
    if not raw:
        return None
    d = json.loads(raw)
    return RouteResult(distance_m=d["distance_m"], duration_s=d["duration_s"],
                       provider=d["provider"], fallback=d.get("fallback", False))


async def put(od_key: str, result: RouteResult) -> None:
    # 兜底结果不缓存太久：Provider 恢复后应尽快回到真实路况
    ttl = 120 if result.fallback else get_settings().maps_cache_ttl
    await set_ttl(_key(od_key), json.dumps({
        "distance_m": result.distance_m, "duration_s": result.duration_s,
        "provider": result.provider, "fallback": result.fallback}), ttl)
