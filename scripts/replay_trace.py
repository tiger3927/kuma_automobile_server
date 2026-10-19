"""轨迹回放：导出某车某时间窗的点（验证里程口径与补报去重）。

    python scripts/replay_trace.py --vin SIM1000000001 --days 1
"""

from __future__ import annotations

import argparse
import asyncio
import json

from src.kuma.core.db import dispose_engine
from src.kuma.geo.mileage import TrackPoint, compute_trip_mileage
from src.kuma.repositories.telemetry_repo import TelemetryRepo
from src.kuma.utils.time import window


async def run(vin: str, days: int) -> None:
    repo = TelemetryRepo()
    start, end = window(days)
    rows = await repo.track(vin, start, end)
    print(json.dumps({"vin": vin, "points": len(rows),
                      "first": rows[0]["ts"].isoformat() if rows else None,
                      "last": rows[-1]["ts"].isoformat() if rows else None}, default=str))
    if len(rows) >= 2:
        points = [TrackPoint(ts=r["ts"].timestamp(), lon=r["lon"], lat=r["lat"],
                             speed_kmh=float(r["speed_kmh"]) if r.get("speed_kmh") else None)
                  for r in rows if r.get("lon") is not None]
        result = await compute_trip_mileage(
            points, start_odo_km=float(rows[0]["odo_km"] or 0),
            end_odo_km=float(rows[-1]["odo_km"] or 0))
        print(json.dumps(result.as_dict(), default=str))
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--vin", required=True)
    p.add_argument("--days", type=int, default=1)
    args = p.parse_args()
    asyncio.run(run(args.vin, args.days))
