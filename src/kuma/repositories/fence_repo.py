"""围栏读写（C4，2026-10-22）。

几何一律走原生 SQL + PostGIS 函数：项目没装 GeoAlchemy，而且 `geo/fence.py` 的包含判定
本来就是 `ST_Contains(f.geom, ST_SetSRID(ST_MakePoint(...), 4326))`——
**写入与读取必须用同一套口径**（SRID 4326 + `geometry`，不是 `geography`；
PostGIS 没有 `ST_Contains(geography, geography)` 重载，2026-10-20 实测过）。

此前 `geo.fence` 没有任何写入方：`fence_exit` 告警（每 600s）和派单硬过滤都依赖这张表，
但围栏只能靠 seed/SQL 配，运营改不了。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from ..core.db import get_engine

# 面积闸门：误画一个跨大洲的多边形，会让"经营区域内"永远为真，派单过滤直接失效。
MAX_AREA_KM2 = 500_000.0
MIN_AREA_KM2 = 0.01


class FenceRepo:
    async def validate_wkt(self, wkt: str) -> dict[str, Any]:
        """把几何校验交给 PostGIS（它比我们在 Python 里解析 WKT 更权威）。"""
        engine = get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT ST_IsValid(g)                       AS valid,
                       GeometryType(g)                     AS gtype,
                       ST_Area(g::geography) / 1e6         AS area_km2,
                       ST_XMin(ST_Envelope(g))             AS xmin,
                       ST_XMax(ST_Envelope(g))             AS xmax,
                       ST_YMin(ST_Envelope(g))             AS ymin,
                       ST_YMax(ST_Envelope(g))             AS ymax
                FROM (SELECT ST_SetSRID(ST_GeomFromText(:wkt), 4326) AS g) t
            """), {"wkt": wkt})).mappings().one()
        return dict(row)

    async def insert(self, *, name: str, fence_type: str, org_id: int | None,
                     region_id: str | None, wkt: str, status: str,
                     active_from: datetime | None, active_to: datetime | None) -> int:
        engine = get_engine()
        async with engine.begin() as conn:
            return (await conn.execute(text("""
                INSERT INTO geo.fence (name, fence_type, org_id, region_id, geom,
                                       status, active_from, active_to)
                VALUES (:name, :fence_type, :org_id, :region_id,
                        ST_SetSRID(ST_GeomFromText(:wkt), 4326),
                        :status, :active_from, :active_to)
                RETURNING id
            """), {"name": name, "fence_type": fence_type, "org_id": org_id,
                   "region_id": region_id, "wkt": wkt, "status": status,
                   "active_from": active_from, "active_to": active_to})).scalar_one()

    async def page(self, *, org_id: int | None = None, region_id: str | None = None,
                   fence_type: str | None = None, status: str | None = None,
                   limit: int = 50, offset: int = 0) -> list[dict]:
        sql = """
            SELECT id, name, fence_type, org_id, region_id, status,
                   active_from, active_to, created_at,
                   ST_Area(geom::geography) / 1e6 AS area_km2,
                   ST_AsText(geom)                AS wkt
            FROM geo.fence WHERE true
        """
        params: dict[str, Any] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if org_id is not None:
            sql += " AND org_id = :org_id"
            params["org_id"] = org_id
        if region_id:
            sql += " AND region_id = :region_id"
            params["region_id"] = region_id
        if fence_type:
            sql += " AND fence_type = :fence_type"
            params["fence_type"] = fence_type
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY id DESC LIMIT :limit OFFSET :offset"
        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def get(self, fence_id: int) -> dict[str, Any] | None:
        engine = get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(text("""
                SELECT id, name, fence_type, org_id, region_id, status,
                       active_from, active_to, created_at,
                       ST_Area(geom::geography) / 1e6 AS area_km2,
                       ST_AsText(geom)                AS wkt
                FROM geo.fence WHERE id = :id
            """), {"id": fence_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def update(self, fence_id: int, *, name: str | None = None, status: str | None = None,
                     active_from: datetime | None = None,
                     active_to: datetime | None = None) -> dict[str, Any] | None:
        sets: list[str] = []
        params: dict[str, Any] = {"id": fence_id}
        for col, val in (("name", name), ("status", status),
                         ("active_from", active_from), ("active_to", active_to)):
            if val is not None:
                sets.append(f"{col} = :{col}")
                params[col] = val
        if not sets:
            return await self.get(fence_id)
        engine = get_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                f"UPDATE geo.fence SET {', '.join(sets)} WHERE id = :id RETURNING id"  # noqa: S608
            ), params)).mappings().one_or_none()
        return await self.get(fence_id) if row else None
