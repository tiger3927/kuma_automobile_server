"""Provider 路由：缓存 → 地区对应的 Provider → 直线距离（带 `pricing_fallback`）。

**中国大陆走高德，其它地区走 Google**（`Task-Server.md` §10.2）。两侧不能互为兜底：

- 高德海外不可用——2026-10-20 实测 TH/ZA/NG 的 `v3/direction/driving` 全部
  `INSUFFICIENT_ABROAD_PRIVILEGES (20011)`，账号级缺权限、换 key 无用；
- Google 在中国大陆无路网且服务不可用。

地区真值的单一来源是 `core.region.features`（seed 注释明确"禁止代码写死国家判断"）：
调用方拿到 features 后用 `providers=` 传进来；没传时按 `region_id`（车辆/行程上就有）查
下面的 v1 默认表，查不到一律按国外处理。

**不做坐标推断**：矩形粗框无法区分中国与东南亚——曼谷（100.5, 13.75）、河内
（105.85, 21.03）都落在中国框内，会把泰国车判成中国去打必然失败的高德
（2026-10-20 加推断时就是这条测试抓出来的）。
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

CN = "CN"

# 地区 → Provider 优先级（v1 默认表；地区真值应落在 `core.region.features.map_providers`，
# 那条迁移落地后用 `providers=` 覆盖，这张表随之退役）
REGION_PREFERENCE: dict[str, list[str]] = {
    CN: ["amap"],        # 中国大陆只有高德能用，Google 不做兜底
    "TH": ["google"],
    "ZA": ["google"],
    "NG": ["google"],
}
FOREIGN_PREFERENCE: list[str] = ["google"]   # 国外不把高德当兜底（海外 20011，白耗配额）


def providers_for(region_id: str | None) -> list[str]:
    """地区 → 该打的 Provider 顺序；未知地区与缺省一律按国外（Google）处理。"""
    if not region_id:
        return list(FOREIGN_PREFERENCE)
    return list(REGION_PREFERENCE.get(region_id, FOREIGN_PREFERENCE))


async def route(a: tuple[float, float], b: tuple[float, float], *,
                region_id: str | None = None, providers: list[str] | None = None,
                mode: str = "driving", bucket: str = "flat") -> RouteResult:
    key = od_key(a, b, mode=mode, bucket=bucket)
    cached = await cache.get(key)
    if cached:
        log.info("chooser.cache_hit", provider=cached.provider, distance_m=cached.distance_m)
        return cached

    names = list(providers) if providers else providers_for(region_id)
    log.info("chooser.pick", region_id=region_id or "none", providers=",".join(names))

    for name in names:
        provider = _PROVIDERS.get(name)
        if provider is None:
            log.warn("chooser.unknown_provider", provider=name)
            continue
        if not await quota.try_consume(name):
            log.warn("chooser.quota_exhausted", provider=name)
            continue
        result = await provider.route(a, b)
        if result is not None:
            log.info("chooser.hit", provider=result.provider, distance_m=result.distance_m,
                     duration_s=result.duration_s)
            await cache.put(key, result)
            return result
        log.warn("chooser.provider_failed", provider=name)

    fallback = straight_line(a, b)
    log.warn("chooser.fallback", region_id=region_id or "none", distance_m=fallback.distance_m)
    await cache.put(key, fallback)
    return fallback
