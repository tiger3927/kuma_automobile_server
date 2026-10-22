"""车辆主数据路由（M2 验收：分页过滤可用、经销商只见自有车、凭据可重置）。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep, client_ip
from ...core.pagination import PageParams
from ...schemas.vehicle import (
    DeviceBindIn,
    StatusChangeIn,
    SubscriptionIn,
    TransferIn,
    VehicleCreateIn,
)
from ...services.vehicle_service import VehicleService

router = APIRouter(prefix="/vehicles", tags=["vehicle"])
svc = VehicleService()


@router.get("")
async def list_vehicles(session: Db, principal: PrincipalDep, q: str | None = None,
                        status: str | None = None, type_code: str | None = None,
                        region_id: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    return await svc.page(session, principal, q=q, status=status, type_code=type_code,
                          region_id=region_id, page=PageParams(limit=limit, offset=offset))


@router.get("/types")
async def vehicle_types(session: Db, principal: PrincipalDep,
                        region_id: str | None = None) -> dict:
    rows = await svc.repo.types(session, region_id)
    return {"items": [{"type_code": t.type_code, "name": t.name, "product_line": t.product_line,
                       "category": t.category, "seats": t.seats, "passenger_seats": t.passenger_seats,
                       "battery_kwh": t.battery_kwh, "range_km": t.range_km,
                       "specs": t.specs, "features": t.features} for t in rows]}


@router.get("/plans")
async def subscription_plans(session: Db, principal: PrincipalDep,
                             region_id: str | None = None) -> dict:
    rows = await svc.repo.plans(session, region_id)
    return {"items": [{"id": p.id, "code": p.code, "plan_type": p.plan_type, "price": p.price,
                       "currency": p.currency, "duration_days": p.duration_days} for p in rows]}


@router.get("/breakdown")
async def breakdown(session: Db, principal: PrincipalDep) -> dict:
    return {"items": await svc.breakdown(session, principal)}


@router.get("/{vin}")
async def get_vehicle(vin: str, session: Db, principal: PrincipalDep) -> dict:
    return await svc.get(session, principal, vin)


@router.post("", status_code=201)
async def create_vehicle(body: VehicleCreateIn, request: Request, session: Db,
                         principal: PrincipalDep, _idem: IdempotencyDep) -> dict:
    vehicle = await svc.create(session, principal, **body.model_dump(), ip=client_ip(request))
    return {"id": vehicle.id, "vin": vehicle.vin, "status": vehicle.status}


@router.patch("/{vin}/status")
async def change_status(vin: str, body: StatusChangeIn, request: Request, session: Db,
                        principal: PrincipalDep) -> dict:
    vehicle = await svc.change_status(session, principal, vin, body.status, ip=client_ip(request))
    return {"vin": vin, "status": vehicle.status}


@router.post("/{vin}/devices")
async def bind_device(vin: str, body: DeviceBindIn, request: Request, session: Db,
                      principal: PrincipalDep) -> dict:
    device = await svc.bind_device(session, principal, vin, device_sn=body.device_sn,
                                   iccid=body.iccid, model=body.model, ip=client_ip(request))
    return {"device_sn": device.device_sn, "vin": vin}


@router.post("/{vin}/credential")
async def issue_credential(vin: str, request: Request, session: Db,
                           principal: PrincipalDep) -> dict:
    """下发 T-Box 凭据（C5）。明文口令**只在这次响应里出现一次**，库里只有哈希。"""
    return await svc.issue_credential(session, principal, vin, ip=client_ip(request))


@router.get("/{vin}/credential")
async def credential_status(vin: str, session: Db, principal: PrincipalDep) -> dict:
    """凭据状态（版本/状态/签发与吊销时间）——不含口令与哈希。"""
    return await svc.credential_status(session, principal, vin)


@router.post("/{vin}/credential/rotate")
async def rotate_credential(vin: str, request: Request, session: Db,
                            principal: PrincipalDep) -> dict:
    """轮换：口令泄露或设备转手。旧口令立即失效（MQTT 每次连接都回库校验）。"""
    return await svc.rotate_credential(session, principal, vin, ip=client_ip(request))


@router.delete("/{vin}/credential")
async def revoke_credential(vin: str, request: Request, session: Db,
                            principal: PrincipalDep) -> dict:
    """吊销：此后该 VIN 的 MQTT 连接与 publish 都会被拒。"""
    return await svc.revoke_credential(session, principal, vin, ip=client_ip(request))


@router.post("/{vin}/subscription")
async def activate_subscription(vin: str, body: SubscriptionIn, request: Request, session: Db,
                                principal: PrincipalDep, _idem: IdempotencyDep) -> dict:
    sub = await svc.activate_subscription(session, principal, vin, body.plan_id,
                                          ip=client_ip(request))
    return {"id": sub.id, "vin": vin, "status": sub.status}


@router.get("/{vin}/subscription")
async def get_subscription(vin: str, session: Db, principal: PrincipalDep) -> dict:
    """订阅历史与当前状态（C6：此前没有任何读取路径）。"""
    return await svc.subscription_detail(session, principal, vin)


@router.post("/{vin}/subscription/cancel")
async def cancel_subscription(vin: str, request: Request, session: Db,
                              principal: PrincipalDep) -> dict:
    return await svc.cancel_subscription(session, principal, vin, ip=client_ip(request))


@router.post("/{vin}/transfer")
async def transfer(vin: str, body: TransferIn, request: Request, session: Db,
                   principal: PrincipalDep) -> dict:
    return await svc.transfer_ownership(session, principal, vin, **body.model_dump(),
                                        ip=client_ip(request))


@router.get("/{vin}/track")
async def track(vin: str, session: Db, principal: PrincipalDep,
                start: str | None = Query(default=None),
                end: str | None = Query(default=None), limit: int = 5000) -> dict:
    from datetime import UTC, datetime, timedelta

    from ...services.telemetry_service import TelemetryService

    principal.require("vehicle_trace", "read")
    await svc.get(session, principal, vin)          # 先过 scope 可见性，越权不返回数据
    end_dt = datetime.fromisoformat(end) if end else datetime.now(UTC)
    start_dt = (datetime.fromisoformat(start) if start
                else end_dt - timedelta(days=1))
    items = await TelemetryService().track(vin, start_dt, end_dt, limit=limit)
    return {"vin": vin, "start": start_dt.isoformat(), "end": end_dt.isoformat(), "items": items}
