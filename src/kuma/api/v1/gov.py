"""政府监管只读接口（scope = `all_readonly`，不做物理隔离，Task-Plan.md §9.6）。

**每次访问都写审计（含查询条件）**——这是合规硬要求，不是可选项（`audit.audit_log.query` 列就是为它存在的）。

2026-10-22 补齐两件事：
1. 此前只有 trips / trip_detail / sales 三个端点，监管要看车辆、轨迹、告警、下行指令都拿不到；
2. 此前用 `request.state.principal` 取身份，绕过了 `PrincipalDep` 统一依赖（能工作，但鉴权路径不一致）。
现在全部走 `PrincipalDep`，读取集中在 `GovRepo`（跨 schema join 只存在于 `rep` 视图里）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request

from ...core.audit import audit_regulator_query
from ...core.deps import Db, PrincipalDep, client_ip
from ...core.exceptions import Forbidden
from ...core.pagination import PageParams, check_time_window
from ...repositories.gov_repo import GovRepo
from ...repositories.stats_repo import StatsRepo
from ...services.audit_service import AuditService
from ...services.telemetry_service import TelemetryService
from ...services.trip_service import TripService

router = APIRouter(prefix="/gov", tags=["gov"])

gov_repo = GovRepo()
audit_svc = AuditService()


def _require_regulator(principal: PrincipalDep) -> None:
    if not principal.is_regulator and not principal.is_platform:
        raise Forbidden("regulator_only")


async def _stamp(session: Db, principal, request: Request, *, action: str,
                 resource_type: str, params: dict) -> None:
    """留痕 + 记录"什么时候查的"。所有 gov 端点的第一动作，漏了就是合规事故。"""
    await audit_regulator_query(
        session, actor=principal, action=action, resource_type=resource_type,
        params={**params, "as_of": datetime.now(UTC).isoformat()}, ip=client_ip(request))


@router.get("/trips")
async def trips(request: Request, session: Db, principal: PrincipalDep,
                region_id: str | None = None, vin: str | None = None,
                since: datetime | None = None, until: datetime | None = None,
                limit: int = 200, offset: int = 0) -> dict:
    _require_regulator(principal)
    params = {"region_id": region_id, "vin": vin, "since": since, "until": until,
              "limit": limit, "offset": offset}
    await _stamp(session, principal, request, action="gov.trips.read",
                 resource_type="trip", params=params)
    return {"items": await gov_repo.trips(region_id=region_id, vin=vin, since=since,
                                          until=until, limit=limit, offset=offset)}


@router.get("/trips/{trip_id}")
async def trip_detail(trip_id: int, request: Request, session: Db,
                      principal: PrincipalDep) -> dict:
    _require_regulator(principal)
    await _stamp(session, principal, request, action="gov.trip.read",
                 resource_type="trip", params={"trip_id": trip_id})
    return await TripService().detail(session, principal, trip_id)


@router.get("/vehicles")
async def vehicles(request: Request, session: Db, principal: PrincipalDep,
                   region_id: str | None = None, status: str | None = None,
                   vin: str | None = None, sub_status: str | None = None,
                   limit: int = 50, offset: int = 0) -> dict:
    """车辆台账：含凭据下发状态与订阅状态（监管查"这辆车有没有被非法控制"的入口）。"""
    _require_regulator(principal)
    params = {"region_id": region_id, "status": status, "vin": vin, "sub_status": sub_status,
              "limit": limit, "offset": offset}
    await _stamp(session, principal, request, action="gov.vehicles.read",
                 resource_type="vehicle", params=params)
    return {"items": await gov_repo.vehicles(region_id=region_id, status=status, vin=vin,
                                             sub_status=sub_status, limit=limit, offset=offset)}


@router.get("/vehicles/{vin}/track")
async def vehicle_track(vin: str, start: datetime, end: datetime, request: Request, session: Db,
                        principal: PrincipalDep, limit: int = 5000) -> dict:
    """轨迹：必须带时间窗（`check_time_window`，默认上限 `KUMA_TRACE_MAX_DAYS`）。"""
    _require_regulator(principal)
    check_time_window(start, end)
    await _stamp(session, principal, request, action="gov.track.read",
                 resource_type="telemetry",
                 params={"vin": vin, "start": start, "end": end, "limit": limit})
    return {"vin": vin, "items": await TelemetryService().track(vin, start, end, limit=limit)}


@router.get("/alarms")
async def alarms(request: Request, session: Db, principal: PrincipalDep,
                 region_id: str | None = None, type: str | None = None,
                 status: str | None = None, since: datetime | None = None,
                 until: datetime | None = None, limit: int = 100, offset: int = 0) -> dict:
    _require_regulator(principal)
    params = {"region_id": region_id, "type": type, "status": status, "since": since,
              "until": until, "limit": limit, "offset": offset}
    await _stamp(session, principal, request, action="gov.alarms.read",
                 resource_type="alarm", params=params)
    return {"items": await gov_repo.alarms(region_id=region_id, type_=type, status=status,
                                           since=since, until=until, limit=limit, offset=offset)}


@router.get("/commands")
async def commands(request: Request, session: Db, principal: PrincipalDep,
                   region_id: str | None = None, vin: str | None = None,
                   status: str | None = None, since: datetime | None = None,
                   until: datetime | None = None, limit: int = 100, offset: int = 0) -> dict:
    """下行指令：含最后一次 `command_audit` 事件——监管要能回答"谁在什么时候对哪辆车断了电"。"""
    _require_regulator(principal)
    params = {"region_id": region_id, "vin": vin, "status": status, "since": since,
              "until": until, "limit": limit, "offset": offset}
    await _stamp(session, principal, request, action="gov.commands.read",
                 resource_type="command", params=params)
    return {"items": await gov_repo.commands(region_id=region_id, status=status, vin=vin,
                                             since=since, until=until, limit=limit, offset=offset)}


@router.get("/audit")
async def audit_trail(request: Request, session: Db, principal: PrincipalDep,
                      actor_account_id: int | None = None, action: str | None = None,
                      resource_type: str | None = None, resource_id: str | None = None,
                      since: datetime | None = None, until: datetime | None = None,
                      limit: int = 50, offset: int = 0) -> dict:
    """监管自查审计（`R_AUDIT` 已给 regulator 只读，见 `core/scope.py:96`）。"""
    _require_regulator(principal)
    return await audit_svc.page(session, principal, actor_account_id=actor_account_id,
                                action=action, resource_type=resource_type,
                                resource_id=resource_id, since=since, until=until,
                                page=PageParams(limit=limit, offset=offset),
                                ip=client_ip(request))


@router.get("/reports/sales")
async def sales(request: Request, session: Db, principal: PrincipalDep) -> dict:
    _require_regulator(principal)
    await _stamp(session, principal, request, action="gov.sales.read",
                 resource_type="report", params={})
    return {"items": await StatsRepo().sales_by_region()}
