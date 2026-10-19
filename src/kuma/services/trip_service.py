"""行程查询与里程核算（口径调用 `geo/mileage.py`，本层不实现算法）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import NotFound
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..geo.clean import TrackPoint
from ..geo.mileage import compute_trip_mileage
from ..repositories.telemetry_repo import TelemetryRepo
from ..repositories.trip_repo import TripRepo


class TripService:
    repo = TripRepo()
    tele = TelemetryRepo()

    async def page(self, session: AsyncSession, principal: Principal, *, vin: str | None = None,
                   status: str | None = None, start: datetime | None = None,
                   end: datetime | None = None, page: PageParams | None = None) -> dict:
        rows = (await session.execute(
            self.repo.page(principal, vin=vin, status=status, start=start, end=end, page=page)
        )).scalars().all()
        return {"items": [_trip_dict(t) for t in rows],
                "limit": (page or PageParams()).limit, "offset": (page or PageParams()).offset}

    async def detail(self, session: AsyncSession, principal: Principal, trip_id: int) -> dict:
        trip = await self.repo.get(session, trip_id)
        if trip is None:
            raise NotFound("trip_not_found")
        if not _matches(trip, principal):
            raise NotFound("trip_not_found")
        mileage = await self.repo.mileage(session, trip_id)
        return {**_trip_dict(trip),
                "mileage": [{"method": m.method, "distance_m": m.distance_m,
                             "confidence": m.confidence, "flags": m.flags} for m in mileage]}

    async def recalc_mileage(self, session: AsyncSession, trip_id: int, *,
                             actor: Principal | None = None) -> dict:
        """worker 用：按三种口径重算并落 `biz.trip_mileage`；差异 >15% 交给 alarm_rules。"""
        trip = await self.repo.get(session, trip_id)
        if trip is None:
            raise NotFound("trip_not_found")
        rows = await self.tele.track(trip.vin, trip.start_ts, trip.end_ts or trip.start_ts)
        points = [TrackPoint(ts=r["ts"].timestamp(), lon=r["lon"], lat=r["lat"],
                             speed_kmh=_f(r.get("speed_kmh")))
                  for r in rows if r.get("lon") is not None]
        result = await compute_trip_mileage(
            points, start_odo_km=trip.start_odo_km, end_odo_km=trip.end_odo_km)

        if result.odo_m is not None:
            await self.repo.save_mileage(session, trip_id, "odo", result.odo_m, confidence=0.95)
        if result.gps_m is not None:
            await self.repo.save_mileage(
                session, trip_id, "gps", result.gps_m,
                confidence=0.7, flags={"anomaly": result.anomaly, "ratio": result.ratio})
        if actor is not None:
            await write_audit(session, actor=actor, action="trip.recalc_mileage",
                              resource_type="trip", resource_id=trip_id, after=result.as_dict())
        return result.as_dict()


def _f(v) -> float | None:
    return None if v is None else float(v)


def _matches(trip, principal: Principal) -> bool:
    if principal.is_platform or principal.is_regulator:
        return True
    if trip.owner_account_id == principal.account_id:
        return True
    if trip.driver_account_id == principal.account_id:
        return True
    return trip.operator_org_id in principal.org_ids("operator")


def _trip_dict(t) -> dict:
    return {
        "id": t.id, "vin": t.vin, "start_ts": t.start_ts, "end_ts": t.end_ts,
        "status": t.status, "start_odo_km": t.start_odo_km, "end_odo_km": t.end_odo_km,
        "owner_account_id": t.owner_account_id, "operator_org_id": t.operator_org_id,
        "driver_account_id": t.driver_account_id, "region_id": t.region_id,
    }
