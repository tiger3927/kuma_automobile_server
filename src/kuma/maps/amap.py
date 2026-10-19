"""高德（中国/东南亚兜底）。"""

from __future__ import annotations

import httpx

from ..core.config import get_settings
from ..core.logging import get_logger
from .base import MapProvider, RouteResult

log = get_logger("maps.amap")
BASE = "https://restapi.amap.com/v3/direction/driving"


class AmapProvider(MapProvider):
    name = "amap"

    async def route(self, a, b) -> RouteResult | None:
        key = get_settings().amap_key
        if not key:
            return None
        params = {
            "origin": f"{a[0]},{a[1]}", "destination": f"{b[0]},{b[1]}",
            "strategy": "10", "key": key, "output": "JSON",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                data = (await client.get(BASE, params=params)).json()
        except Exception as exc:
            log.warn("amap.failed", err=str(exc))
            return None
        if data.get("status") != "1" or not data.get("route", {}).get("paths"):
            log.warn("amap.not_ok", info=data.get("info"))
            return None
        path = data["route"]["paths"][0]
        return RouteResult(distance_m=int(path["distance"]), duration_s=int(path["duration"]),
                           provider=self.name)
