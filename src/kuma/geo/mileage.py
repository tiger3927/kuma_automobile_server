"""里程口径的**唯一实现**（Task-Server.md §11）：计价、报表、政府上报三处共用。

| 口径 | 算法 | 用途 |
|---|---|---|
| `odo_mileage` | 车端累计里程差值 | 结算 / 报表 / 政府上报主口径 |
| `gps_mileage` | 抽稀后 `ST_Length(geography)` | 校验、无 odo 兜底、拼车区间计价 |
| `road_mileage` | 贴路（v1 不做自研 map matching） | 仅抽样校准 |

差异 > 15% → `mileage_anomaly` 告警。
"""

from __future__ import annotations

from dataclasses import dataclass

from shapely.geometry import LineString
from sqlalchemy import text

from ..core.db import get_engine
from ..core.logging import get_logger
from .clean import TrackPoint, drop_drift, simplify

log = get_logger("geo.mileage")

ANOMALY_RATIO = 0.15
SQL_FILE = "src/kuma/geo/sql/mileage.sql"


@dataclass(slots=True)
class MileageResult:
    odo_m: int | None
    gps_m: int | None
    method_used: str            # odo | gps
    anomaly: bool
    ratio: float | None

    def as_dict(self) -> dict:
        return {"odo_m": self.odo_m, "gps_m": self.gps_m, "method_used": self.method_used,
                "anomaly": self.anomaly, "ratio": self.ratio}


def odo_mileage(start_odo_km: float | None, end_odo_km: float | None) -> int | None:
    if start_odo_km is None or end_odo_km is None or end_odo_km < start_odo_km:
        return None
    return round((end_odo_km - start_odo_km) * 1000)   # round() 无 ndigits 已是 int


def to_linestring_wkt(points: list[TrackPoint]) -> str | None:
    """抽稀后的点集 → LINESTRING WKT（PostGIS geography 计算用）。"""
    cleaned = simplify(drop_drift(points), tolerance_m=3.0)
    if len(cleaned) < 2:
        return None
    line = LineString([(p.lon, p.lat) for p in cleaned])
    if not line.is_valid:
        line = line.buffer(0).boundary
    coords = " ".join(f"{x} {y}" for x, y in line.coords)
    return f"SRID=4326;LINESTRING({coords})"


async def gps_mileage(points: list[TrackPoint]) -> int | None:
    """走 PostGIS geography 计算（球面长度，米）。"""
    wkt = to_linestring_wkt(points)
    if wkt is None:
        return None
    engine = get_engine()
    async with engine.connect() as conn:
        meters = (await conn.execute(
            text("SELECT round(ST_Length(ST_GeogFromText(:wkt)))::int"), {"wkt": wkt}
        )).scalar_one_or_none()
    return int(meters) if meters is not None else None


async def compute_trip_mileage(points: list[TrackPoint], *,
                               start_odo_km: float | None, end_odo_km: float | None) -> MileageResult:
    """一次算齐三种口径，并给出异常标记（§11）。"""
    odo = odo_mileage(start_odo_km, end_odo_km)
    gps = await gps_mileage(points)

    anomaly = False
    ratio: float | None = None
    if odo and gps and odo > 0:
        ratio = abs(gps - odo) / odo
        anomaly = ratio > ANOMALY_RATIO

    if odo:
        return MileageResult(odo, gps, "odo", anomaly, None if ratio is None else round(ratio, 4))
    return MileageResult(odo, gps, "gps", False, None)
