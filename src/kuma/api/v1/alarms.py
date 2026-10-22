"""告警查询与确认（M5 验收：告警可查可确认）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...core.pagination import PageParams
from ...schemas.ops import AckIn, CloseIn, ReopenIn, SilenceIn
from ...services.alarm_service import AlarmService

router = APIRouter(prefix="/alarms", tags=["alarm"])
svc = AlarmService()


@router.get("")
async def list_alarms(session: Db, principal: PrincipalDep, vin: str | None = None,
                      status: str | None = None, type: str | None = None,
                      limit: int = 20, offset: int = 0) -> dict:
    return await svc.page(session, principal, vin=vin, status=status, type_=type,
                          page=PageParams(limit=limit, offset=offset))


@router.get("/hotspots")
async def hotspots(session: Db, principal: PrincipalDep) -> dict:
    return {"items": await svc.hotspots(session, principal)}


@router.post("/{alarm_id}/ack")
async def ack(alarm_id: int, body: AckIn, request: Request, session: Db,
              principal: PrincipalDep) -> dict:
    return await svc.ack(session, principal, alarm_id, body.note, ip=client_ip(request))


@router.post("/{alarm_id}/silence")
async def silence(alarm_id: int, body: SilenceIn, request: Request, session: Db,
                  principal: PrincipalDep) -> dict:
    """静默：窗口内同类告警不再新建（抑制逻辑在 `AlarmRepo.raise_alarm`）。"""
    return await svc.transition(session, principal, alarm_id, "silenced",
                                note=body.note, snooze_s=body.snooze_s, ip=client_ip(request))


@router.post("/{alarm_id}/close")
async def close(alarm_id: int, body: CloseIn, request: Request, session: Db,
                principal: PrincipalDep) -> dict:
    return await svc.transition(session, principal, alarm_id, "closed",
                                note=body.note, ip=client_ip(request))


@router.post("/{alarm_id}/reopen")
async def reopen(alarm_id: int, body: ReopenIn, request: Request, session: Db,
                 principal: PrincipalDep) -> dict:
    """重开必须给 reason——终态被推翻是可追责动作。"""
    return await svc.transition(session, principal, alarm_id, "open",
                                note=body.note, ip=client_ip(request))


@router.get("/{alarm_id}")
async def detail(alarm_id: int, session: Db, principal: PrincipalDep) -> dict:
    """告警详情（含确认记录）。此前 `AlarmService.get` 是孤儿、没有详情端点。"""
    return await svc.detail(session, principal, alarm_id)
