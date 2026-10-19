"""Provider 路由：缓存 → 主 Provider → 兜底 Provider → 直线距离（带 `pricing_fallback`）。

v1 按地区配置选主（`core.region.features` 决定，代码不写死国家）。
"""

from __future__ import annotations

from ..core.logging import get_logger
from ..geo.h3 import od_key
from . import cache, quota
from .amap import AmapProvider
from .base import MapProvider, RouteResult, straight_line
from .google import GoogleProvider

log = get_logger("maps.chooser")

_PROVIDERS: dict[str, MapProvider] = {"google": GoogleProvider(), "amap": AmapProvider()}

# 地区 → Provider 优先级（v1 默认；上线后由 core.region.features 覆盖）
REGION_PREFERENCE: dict[str, list[str]] = {
    "TH": ["google", "amap"],
    "ZA": ["google", "amap"],
    "NG": ["google"],
    "CN": ["amap", "google"],
}


async def route(a: tuple[float, float], b: tuple[float, float], *, region_id: str = "TH",
                mode: str = "driving", bucket: str = "flat") -> RouteResult:
    key = od_key(a, b, mode=mode, bucket=bucket)
    cached = await cache.get(key)
    if cached:
        return cached

    for name in REGION_PREFERENCE.get(region_id, ["google", "amap"]):
        provider = _PROVIDERS.get(name)
        if provider is None:
            continue
        if not await quota.try_consume(name):
            log.warn("chooser.quota_exhausted", provider=name)
            continue
        result = await provider.route(a, b)
        if result is not None:
            await cache.put(key, result)
            return result

    fallback = straight_line(a, b)
    log.warn("chooser.fallback", region_id=region_id, distance_m=fallback.distance_m)
    await cache.put(key, fallback)
    return fallback
