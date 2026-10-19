"""Provider 配额（Redis 计数，按天）：超配额即降级，不打爆第三方账单。"""

from __future__ import annotations

from datetime import UTC, datetime

from ..core.config import get_settings
from ..core.redis_client import get_redis, kkey, set_ttl


def _day_key(provider: str) -> str:
    return kkey("maps", "quota", provider, f"{datetime.now(UTC):%Y%m%d}")


async def used(provider: str) -> int:
    raw = await get_redis().get(_day_key(provider))
    return int(raw) if raw else 0


async def try_consume(provider: str) -> bool:
    s = get_settings()
    key = _day_key(provider)
    count = await get_redis().incr(key)
    if count == 1:
        await set_ttl(key, "1", 90_000)      # 略大于一天，避免跨零点残留
    return count <= s.maps_daily_quota


async def remaining(provider: str) -> int:
    return max(0, get_settings().maps_daily_quota - await used(provider))
