"""行程与里程（M5）。"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep
from ...core.pagination import PageParams
from ...services.trip_service import TripService

router = APIRouter(prefix="/trips", tags=["trip"])
svc = TripService()


@router.get("")
async def list_trips(session: Db, principal: PrincipalDep, vin: str | None = None,
                     status: str | None = None, start: datetime | None = None,
                     end: datetime | None = None, limit: int = 20, offset: int = 0) -> dict:
    return await svc.page(session, principal, vin=vin, status=status, start=start, end=end,
                          page=PageParams(limit=limit, offset=offset))


@router.get("/{trip_id}")
async def trip_detail(trip_id: int, session: Db, principal: PrincipalDep) -> dict:
    return await svc.detail(session, principal, trip_id)


@router.post("/{trip_id}/recalc")
async def recalc(trip_id: int, request: Request, session: Db, principal: PrincipalDep) -> dict:
    principal.require("vehicle_trace", "write")
    return await svc.recalc_mileage(session, trip_id, actor=principal)
