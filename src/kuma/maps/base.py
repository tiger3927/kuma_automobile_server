"""地图 Provider 抽象（Task-Plan.md §8.4 / Task-Server.md §10.2）。

无 key 或 Provider 失败 → 直线距离兜底 + `pricing_fallback` 标记（不静默降级）。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class RouteResult:
    distance_m: int
    duration_s: int
    provider: str
    fallback: bool = False
    raw: dict = field(default_factory=dict)


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> int:
    (lon1, lat1), (lon2, lat2) = a, b
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(h)))


def straight_line(a: tuple[float, float], b: tuple[float, float], *,
                  avg_kmh: float = 22.0) -> RouteResult:
    """三轮车城区均速兜底（口径待 §8.4 定稿后调整）。"""
    dist = haversine_m(a, b)
    return RouteResult(distance_m=dist, duration_s=int(dist / 1000 / avg_kmh * 3600),
                       provider="straight_line", fallback=True)


class MapProvider(ABC):
    name: str

    @abstractmethod
    async def route(self, a: tuple[float, float], b: tuple[float, float]) -> RouteResult | None:
        """返回 None 表示该 Provider 不可用（无 key / 超配额 / 上游错误），由 chooser 降级。"""
