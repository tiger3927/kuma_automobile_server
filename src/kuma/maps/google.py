"""Google Maps Directions（泰国/南非/尼日利亚主力）。"""

from __future__ import annotations

import httpx

from ..core.config import get_settings
from ..core.logging import get_logger
from .base import MapProvider, RouteResult

log = get_logger("maps.google")
BASE = "https://maps.googleapis.com/maps/api/directions/json"


class GoogleProvider(MapProvider):
    name = "google"

    async def route(self, a, b) -> RouteResult | None:
        key = get_settings().google_key
        if not key:
            return None
        params = {
            "origin": f"{a[1]},{a[0]}", "destination": f"{b[1]},{b[0]}",
            "mode": "driving", "key": key, "language": "en",
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(BASE, params=params)
                data = resp.json()
        except Exception as exc:
            log.warn("google.failed", err=str(exc))
            return None
        if data.get("status") != "OK" or not data.get("routes"):
            log.warn("google.not_ok", status=data.get("status"))
            return None
        leg = data["routes"][0]["legs"][0]
        return RouteResult(distance_m=int(leg["distance"]["value"]),
                           duration_s=int(leg["duration"]["value"]),
                           provider=self.name, raw={"status": data.get("status")})
