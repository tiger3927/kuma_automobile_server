"""车辆实时状态（Redis）——**实时位置的唯一来源**（Task-Plan.md §10-10）。

派单、看板、商城"附近的车"都读这里，不查遥测表；gateway 写入，带 TTL（在线判定即 TTL 过期）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ..core.config import get_settings
from ..core.redis_client import get_redis, kkey, set_ttl
from ..models.tele import Telemetry  # noqa: F401  类型参考，实际写 Redis


def state_key(vin: str) -> str:
    return kkey("state", vin)


def online_key(vin: str) -> str:
    return kkey("online", vin)


class StateRepo:
    async def update(self, vin: str, point: dict[str, Any]) -> None:
        ttl = get_settings().state_ttl
        payload = {
            "vin": vin,
            "ts": point["ts"].isoformat() if isinstance(point.get("ts"), datetime) else point.get("ts"),
            "recv_ts": datetime.now(UTC).isoformat(),
            "lat": point.get("lat"), "lon": point.get("lon"),
            "speed_kmh": point.get("speed_kmh"), "soc": point.get("soc"),
            "odo_km": point.get("odo_km"), "quality": point.get("quality", 0),
        }
        await set_ttl(state_key(vin), json.dumps(payload, default=str), ttl)
        await set_ttl(online_key(vin), payload["ts"] or "", ttl)

    async def get(self, vin: str) -> dict[str, Any] | None:
        raw = await get_redis().get(state_key(vin))
        return json.loads(raw) if raw else None

    async def is_online(self, vin: str) -> bool:
        return bool(await get_redis().exists(online_key(vin)))

    async def last_ts(self, vin: str) -> str | None:
        raw = await get_redis().get(online_key(vin))
        # 客户端开了 decode_responses，但 redis-py 的返回类型仍标成 bytes|str|None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def many(self, vins: list[str]) -> dict[str, dict[str, Any]]:
        if not vins:
            return {}
        r = get_redis()
        values = await r.mget([state_key(v) for v in vins])
        return {v: json.loads(raw) for v, raw in zip(vins, values, strict=False) if raw}

    async def set_offline_reason(self, vin: str, reason: str) -> None:
        await set_ttl(kkey("offline_reason", vin), reason, 3600)
