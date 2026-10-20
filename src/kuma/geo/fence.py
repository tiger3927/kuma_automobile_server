"""电子围栏判定（PostGIS，GiST 索引在 `geo.fence.geom`）。

运营商获准经营区域是派单硬过滤条件（Task-Plan.md §8.4）；禁行区与告警区走 worker 规则。

⚠️ 包含判定走 **geometry 平面**，不走 geography：PostGIS 的 `ST_Contains` 没有 geography 重载
（2026-10-20 实测 `function st_contains(geography, geography) does not exist`，这条查询在
有围栏数据后必然报错，此前因为从没建过围栏而没暴露）。围栏是城市尺度，经纬度平面判定的
误差可忽略；**距离**计算仍走 geography（见 `distance_to_fence`）。
"""

from __future__ import annotations

from sqlalchemy import text

from ..core.db import get_engine


async def fences_containing(lon: float, lon_lat: float | None = None, *, lat: float | None = None,
                            org_id: int | None = None, fence_type: str | None = None) -> list[dict]:
    lat = lat if lat is not None else lon_lat
    assert lat is not None
    engine = get_engine()
    sql = """
        SELECT f.id, f.name, f.fence_type, f.org_id, f.region_id
        FROM geo.fence f
        WHERE f.status = 'active'
          AND ST_Contains(f.geom, ST_SetSRID(ST_MakePoint(:lon, :lat)::geometry, 4326))
          AND (f.active_from IS NULL OR f.active_from <= now())
          AND (f.active_to IS NULL OR f.active_to >= now())
    """
    params: dict = {"lon": lon, "lat": lat}
    if org_id is not None:
        sql += " AND f.org_id = :org_id"
        params["org_id"] = org_id
    if fence_type:
        sql += " AND f.fence_type = :fence_type"
        params["fence_type"] = fence_type
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def inside_operating_area(lon: float, lat: float, org_id: int) -> bool:
    rows = await fences_containing(lon, lat=lat, org_id=org_id, fence_type="operating_area")
    return bool(rows)


async def distance_to_fence(lon: float, lat: float, fence_id: int) -> float | None:
    engine = get_engine()
    async with engine.connect() as conn:
        meters = (await conn.execute(text(
            """
            SELECT round(ST_Distance(
                ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
                f.geom::geography)::int)
            FROM geo.fence f WHERE f.id = :fence_id
            """
        ), {"lon": lon, "lat": lat, "fence_id": fence_id})).scalar_one_or_none()
    return float(meters) if meters is not None else None
