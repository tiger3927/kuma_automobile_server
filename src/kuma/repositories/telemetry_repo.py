"""遥测读写**唯一出口**（Task-Server.md §7 铁律 4）。

将来切 ClickHouse/Timescale 只改这一个文件。写入路径：
asyncpg COPY（EWKT 文本）→ `tele.telemetry_staging`
→ `INSERT ... ST_GeomFromEWKT(geom) ... ON CONFLICT (vin,ts) DO NOTHING` → TRUNCATE 中转表。
（§12-5 禁逐条 INSERT；§8.8 补报按 (vin,ts) 去重；
中转表 geom 是 text——asyncpg COPY 无 geometry 编码器，见迁移 0003）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from ..core.db import get_engine, get_maintenance_engine
from ..core.logging import get_logger
from ..core.pagination import check_time_window

log = get_logger("telemetry_repo")

_COLUMNS = ("vin", "ts", "geom", "speed_kmh", "soc", "odo_km", "seq", "proto", "recv_ts", "quality")


@dataclass(slots=True)
class TelemetryPoint:
    vin: str
    ts: datetime
    lat: float | None
    lon: float | None
    speed_kmh: float | None
    soc: float | None
    odo_km: float | None
    seq: int | None
    proto: str
    recv_ts: datetime
    quality: int = 0
    last: dict | None = None      # 该车上一次状态（gateway 解码时填充，供 deadband 判断）

    def wkt(self) -> str | None:
        if self.lat is None or self.lon is None:
            return None
        return f"SRID=4326;POINT({self.lon} {self.lat})"

    def as_record(self) -> tuple:
        return (self.vin, self.ts, self.wkt(), self.speed_kmh, self.soc, self.odo_km,
                self.seq, self.proto, self.recv_ts, self.quality)


@dataclass(slots=True)
class FlushStats:
    received: int
    inserted: int
    duplicated: int
    rejected_stale: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"received": self.received, "inserted": self.inserted,
                "duplicated": self.duplicated, "rejected_stale": self.rejected_stale}


class TelemetryRepo:
    async def flush(self, points: list[TelemetryPoint]) -> FlushStats:
        if not points:
            return FlushStats(0, 0, 0)
        engine = get_engine()
        async with engine.begin() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            if raw is None:
                raise RuntimeError("拿不到 asyncpg 原生连接，COPY 无法执行")
            # asyncpg 的参数名是 schema_name（实测 schema= 直接 TypeError）
            await raw.copy_records_to_table(
                "telemetry_staging", schema_name="tele", columns=list(_COLUMNS),
                records=[p.as_record() for p in points],
            )
            result = await conn.execute(text(
                """
                INSERT INTO tele.telemetry (vin, ts, geom, speed_kmh, soc, odo_km, seq, proto, recv_ts, quality)
                SELECT vin, ts,
                       CASE WHEN geom IS NULL OR geom = '' THEN NULL
                            ELSE ST_SetSRID(ST_GeomFromEWKT(geom), 4326) END,
                       speed_kmh, soc, odo_km, seq, proto, recv_ts, quality
                FROM tele.telemetry_staging
                ON CONFLICT (vin, ts) DO NOTHING
                """
            ))
            inserted = result.rowcount or 0
            await conn.execute(text("TRUNCATE tele.telemetry_staging"))
        total = len(points)
        return FlushStats(received=total, inserted=inserted, duplicated=total - inserted)

    async def track(self, vin: str, start: datetime, end: datetime,
                    *, limit: int = 5000) -> list[dict]:
        """轨迹回放：必须限窗（≤7 天）+ 限量，否则 512MB PG 会 OOM。"""
        check_time_window(start, end)
        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                """
                SELECT ts, ST_X(geom) AS lon, ST_Y(geom) AS lat,
                       speed_kmh, soc, odo_km, seq, quality
                FROM tele.telemetry
                WHERE vin = :vin AND ts >= :start AND ts < :end
                ORDER BY ts
                LIMIT :limit
                """
            ), {"vin": vin, "start": start, "end": end, "limit": min(limit, 20_000)})).mappings().all()
        return [dict(r) for r in rows]

    async def last_point(self, vin: str) -> dict | None:
        engine = get_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                """
                SELECT ts, ST_X(geom) AS lon, ST_Y(geom) AS lat, speed_kmh, soc, odo_km
                FROM tele.telemetry WHERE vin = :vin ORDER BY ts DESC LIMIT 1
                """
            ), {"vin": vin})).mappings().first()
        return dict(row) if row else None

    async def refresh_hourly(self) -> None:
        # REFRESH 只有物化视图 owner 能干（PG 没有 GRANT REFRESH）→ 走维护通道。
        # **必须 `engine.begin()`，不能 `engine.connect()`**：SQLAlchemy 2.0 的隐式事务
        # 在连接关闭时回滚未提交内容，REFRESH 会被整个撤销，而日志照样打
        # `hourly_refreshed`——worker 每小时"成功"，物化视图却永远是旧的
        # （2026-10-26 接 fleet/stats 实测：`tele.telemetry` 1705 行，matview 只有 4 行；
        # 换成 begin() 后立刻变 9 行、最新小时对上）。同文件 `drop_partition` 用的就是 begin()。
        engine = get_maintenance_engine()
        async with engine.begin() as conn:
            await conn.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY rep.telemetry_hourly"))
        log.info("telemetry_repo.hourly_refreshed")

    async def drop_partition(self, table: str) -> None:
        """保留策略走 DROP PARTITION（§5.3：禁止 DELETE）；DDL 走维护通道（kuma_owner）。"""
        if not table.startswith("telemetry_"):
            raise ValueError("只允许删除遥测分区")
        engine = get_maintenance_engine()
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS tele.{table}"))
        log.info("telemetry_repo.partition_dropped", table=table)

    async def list_partitions(self) -> list[str]:
        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                """
                SELECT c.relname FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE i.inhparent = 'tele.telemetry'::regclass ORDER BY 1
                """
            ))).scalars().all()
        return list(rows)
