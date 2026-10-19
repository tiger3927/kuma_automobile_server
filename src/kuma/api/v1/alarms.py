"""告警查询与确认（M5 验收：告警可查可确认）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...core.pagination import PageParams
from ...schemas.ops import AckIn
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
