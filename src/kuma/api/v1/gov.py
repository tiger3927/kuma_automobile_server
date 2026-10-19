"""政府监管只读接口（scope = `all_readonly`，不做物理隔离，Task-Plan.md §9.6）。

**每次访问写审计（含查询条件）**——这是合规硬要求，不是可选项。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ...core.audit import audit_regulator_query
from ...core.deps import Db, PrincipalDep, client_ip
from ...core.exceptions import Forbidden
from ...repositories.stats_repo import StatsRepo
from ...services.trip_service import TripService

router = APIRouter(prefix="/gov", tags=["gov"])


def _require_regulator(principal: PrincipalDep) -> None:
    if not principal.is_regulator and not principal.is_platform:
        raise Forbidden("regulator_only")


@router.get("/trips")
async def trips(request: Request, session: Db, region_id: str | None = None,
                limit: int = 200) -> dict:
    principal = request.state.principal
    _require_regulator(principal)
    params = {"region_id": region_id, "limit": limit,
              "as_of": datetime.now(UTC).isoformat()}
    await audit_regulator_query(session, actor=principal, action="gov.trips.read",
                                resource_type="trip", params=params, ip=client_ip(request))
    return {"items": await StatsRepo().gov_trips(region_id=region_id, limit=limit)}


@router.get("/trips/{trip_id}")
async def trip_detail(trip_id: int, request: Request, session: Db) -> dict:
    principal = request.state.principal
    _require_regulator(principal)
    await audit_regulator_query(session, actor=principal, action="gov.trip.read",
                                resource_type="trip", params={"trip_id": trip_id},
                                ip=client_ip(request))
    return await TripService().detail(session, principal, trip_id)


@router.get("/reports/sales")
async def sales(request: Request, session: Db) -> dict:
    principal = request.state.principal
    _require_regulator(principal)
    await audit_regulator_query(session, actor=principal, action="gov.sales.read",
                                resource_type="report", params={}, ip=client_ip(request))
    return {"items": await StatsRepo().sales_by_region()}
