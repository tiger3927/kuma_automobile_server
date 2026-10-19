"""车队看板：实时位置读 Redis（§10-10），不查遥测表。"""

from __future__ import annotations

from fastapi import APIRouter

from ...core.deps import Db, PrincipalDep
from ...core.pagination import PageParams
from ...repositories.state_repo import StateRepo
from ...repositories.stats_repo import StatsRepo
from ...repositories.vehicle_repo import VehicleRepo

router = APIRouter(prefix="/fleet", tags=["fleet"])


@router.get("/live")
async def live(session: Db, principal: PrincipalDep, limit: int = 200) -> dict:
    vehicles = (await session.execute(
        VehicleRepo().page(principal, page=PageParams(limit=limit))
    )).scalars().all()
    states = await StateRepo().many([v.vin for v in vehicles])
    return {"items": [{"vin": v.vin, "plate": v.plate, "status": v.status,
                       "online": v.vin in states, "live": states.get(v.vin)} for v in vehicles]}


@router.get("/stats")
async def stats(session: Db, principal: PrincipalDep) -> dict:
    vehicles = (await session.execute(
        VehicleRepo().page(principal, page=PageParams(limit=500))
    )).scalars().all()
    states = await StateRepo().many([v.vin for v in vehicles])
    breakdown = await VehicleRepo().status_breakdown(session, principal)
    return {"vehicle_total": len(vehicles), "online": len(states),
            "breakdown": breakdown,
            "hourly": await StatsRepo().telemetry_hourly(limit=24)}
