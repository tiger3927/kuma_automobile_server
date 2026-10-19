"""轨迹清洗：GPS 漂移剔除 + 抽稀（CPU 密集，调用方走 `asyncio.to_thread`，§12-2）。"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString, Point


@dataclass(slots=True)
class TrackPoint:
    ts: float
    lon: float
    lat: float
    speed_kmh: float | None = None


def drop_drift(points: list[TrackPoint], *, max_speed_kmh: float = 160.0) -> list[TrackPoint]:
    """相邻两点隐含速度超过阈值 → 判为漂移点，丢弃后点。"""
    if len(points) < 2:
        return points
    kept = [points[0]]
    for cur in points[1:]:
        prev = kept[-1]
        dt = max(cur.ts - prev.ts, 1e-6)
        dist = Point(prev.lon, prev.lat).distance(Point(cur.lon, cur.lat)) * 111_000  # 粗略度→米
        if dist / dt * 3.6 <= max_speed_kmh:
            kept.append(cur)
    return kept


def simplify(points: list[TrackPoint], *, tolerance_m: float = 3.0) -> list[TrackPoint]:
    """Douglas-Peucker 抽稀（Shapely，自带 GEOS wheel，禁止 GDAL/pyproj）。

    容差按米换算成度：`tolerance_m / 111000`（赤道近似，三轮车精度足够）。
    """
    if len(points) < 3:
        return points
    line = LineString([(p.lon, p.lat) for p in points])
    simplified = line.simplify(tolerance_m / 111_000.0, preserve_topology=True)
    kept_coords = set(zip(simplified.coords.xy[0], simplified.coords.xy[1], strict=False))
    return [p for p in points if (p.lon, p.lat) in kept_coords]
