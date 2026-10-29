"""换电与电池资产 API（M15）。

**router 只做三件事**：解析入参 → 调 service → 返回。换电的四写、闸门、拒单落库
全在 `services/swap_service.py`（`Task-Server.md` §7 依赖铁律 6）；这里出现任何
`UPDATE energy.*` 都是设计违规。

写操作一律带审计（service 内 `write_audit`）；换电额外要求 `Idempotency-Key`
（站点弱网重试不能重复收费、不能重复换绑）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep
from ...schemas.energy import (
    AllowedModelsIn,
    BatteryIn,
    BatteryModelIn,
    BatteryModelPatch,
    BatteryModelStatusIn,
    BatteryStatusIn,
    DepositRegisterIn,
    DepositRuleIn,
    LeaseIn,
    LeaseTerminateIn,
    NearbyQuery,
    OrgBusinessIn,
    RuleApproveIn,
    StationIn,
    StationPatch,
    SwapIn,
    SwapPriceIn,
)
from ...services.battery_model_service import BatteryModelService
from ...services.lease_service import LeaseService
from ...services.station_service import StationService
from ...services.swap_service import SwapService

router = APIRouter(prefix="/energy", tags=["energy"])

models_svc = BatteryModelService()
stations_svc = StationService()
leases_svc = LeaseService()
swaps_svc = SwapService()


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ══ 型号字典（平台录入与审核；运营商只读——白名单与计价都引用它）══════════
@router.get("/models")
async def list_models(principal: PrincipalDep, session: Db,
                      status: str | None = Query(default=None),
                      limit: int = Query(default=100, ge=1, le=200),
                      offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await models_svc.page(session, principal, status=status, limit=limit, offset=offset)


@router.post("/models", status_code=201)
async def create_model(body: BatteryModelIn, request: Request,
                       principal: PrincipalDep, session: Db) -> dict[str, Any]:
    return await models_svc.create(session, principal, body, ip=_ip(request))


@router.get("/models/{model_code}")
async def get_model(model_code: str, principal: PrincipalDep,
                    session: Db) -> dict[str, Any]:
    return await models_svc.detail(session, principal, model_code)


@router.patch("/models/{model_code}")
async def patch_model(model_code: str, body: BatteryModelPatch, request: Request,
                      principal: PrincipalDep,
                      session: Db) -> dict[str, Any]:
    return await models_svc.patch(session, principal, model_code, body, ip=_ip(request))


@router.post("/models/{model_code}/status")
async def set_model_status(model_code: str, body: BatteryModelStatusIn, request: Request,
                           principal: PrincipalDep,
                           session: Db) -> dict[str, Any]:
    return await models_svc.set_status(session, principal, model_code, body, ip=_ip(request))


# ══ 价目与押金规则（运营商申报 → 平台审核）═══════════════════════════════
@router.get("/prices")
async def list_prices(principal: PrincipalDep, session: Db,
                      status: str | None = Query(default=None),
                      model_code: str | None = Query(default=None),
                      region_id: str | None = Query(default=None),
                      station_org_id: int | None = Query(default=None),
                      limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """价目规则翻页（带 `total`）。审核工作台必须先看得见 pending_review 的申报。"""
    return await models_svc.page_prices(session, principal, status=status,
                                        model_code=model_code, region_id=region_id,
                                        station_org_id=station_org_id,
                                        limit=limit, offset=offset)


@router.get("/deposit-rules")
async def list_deposit_rules(principal: PrincipalDep, session: Db,
                             status: str | None = Query(default=None),
                             model_code: str | None = Query(default=None),
                             region_id: str | None = Query(default=None),
                             limit: int = Query(default=50, ge=1, le=200),
                             offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await models_svc.page_deposit_rules(session, principal, status=status,
                                               model_code=model_code, region_id=region_id,
                                               limit=limit, offset=offset)


@router.post("/prices", status_code=201)
async def propose_price(body: SwapPriceIn, request: Request, principal: PrincipalDep,
                        session: Db) -> dict[str, Any]:
    return await models_svc.propose_price(session, principal, body, ip=_ip(request))


@router.post("/deposit-rules", status_code=201)
async def propose_deposit_rule(body: DepositRuleIn, request: Request,
                               principal: PrincipalDep,
                               session: Db) -> dict[str, Any]:
    return await models_svc.propose_deposit_rule(session, principal, body, ip=_ip(request))


@router.post("/rules/{table}/{rule_id}/approve")
async def approve_rule(table: str, rule_id: int, body: RuleApproveIn, request: Request,
                       principal: PrincipalDep,
                       session: Db) -> dict[str, Any]:
    return await models_svc.approve_rule(session, principal, table, rule_id, body,
                                         ip=_ip(request))


# ══ 业务线与牌照（换电 scope 的入口）═════════════════════════════════════
@router.post("/org-business", status_code=201)
async def declare_business(body: OrgBusinessIn, request: Request, principal: PrincipalDep,
                           session: Db) -> dict[str, Any]:
    return await models_svc.declare_business(session, principal, body, ip=_ip(request))


# ══ 换电站（nearby 必须在 {station_id} 之前声明，否则被 int 路径吃掉）═════
@router.get("/stations/nearby")
async def nearby_stations(principal: PrincipalDep, session: Db,
                          q: NearbyQuery = Depends()) -> dict[str, Any]:
    return await stations_svc.nearby(session, principal, q)


@router.get("/stations")
async def list_stations(principal: PrincipalDep, session: Db,
                        status: str | None = Query(default=None),
                        limit: int = Query(default=50, ge=1, le=200),
                        offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await stations_svc.page(session, principal, status=status, limit=limit, offset=offset)


@router.post("/stations", status_code=201)
async def create_station(body: StationIn, request: Request, principal: PrincipalDep,
                         session: Db) -> dict[str, Any]:
    return await stations_svc.create(session, principal, body, ip=_ip(request))


@router.get("/stations/{station_id}")
async def get_station(station_id: int, principal: PrincipalDep,
                      session: Db) -> dict[str, Any]:
    return await stations_svc.detail(session, principal, station_id)


@router.patch("/stations/{station_id}")
async def patch_station(station_id: int, body: StationPatch, request: Request,
                        principal: PrincipalDep,
                        session: Db) -> dict[str, Any]:
    return await stations_svc.patch(session, principal, station_id, body, ip=_ip(request))


@router.put("/stations/{station_id}/allowed-models")
async def set_allowed_models(station_id: int, body: AllowedModelsIn, request: Request,
                             principal: PrincipalDep,
                             session: Db) -> dict[str, Any]:
    return await stations_svc.set_allowed_models(session, principal, station_id, body,
                                                 ip=_ip(request))


@router.get("/stations/{station_id}/stock")
async def station_stock(station_id: int, principal: PrincipalDep,
                        session: Db) -> dict[str, Any]:
    return await stations_svc.stock(session, principal, station_id)


# ══ 电池台账 ══════════════════════════════════════════════════════════════
@router.post("/batteries", status_code=201)
async def register_battery(body: BatteryIn, request: Request, principal: PrincipalDep,
                           session: Db) -> dict[str, Any]:
    return await stations_svc.register_battery(session, principal, body, ip=_ip(request))


@router.get("/batteries")
async def list_batteries(principal: PrincipalDep, session: Db,
                         station_id: int | None = Query(default=None),
                         status: str | None = Query(default=None),
                         model_code: str | None = Query(default=None),
                         q: str | None = Query(default=None, max_length=64),
                         limit: int = Query(default=50, ge=1, le=200),
                         offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """电池台账翻页（带 `total`）。scope 按**归属运营商**收敛，不是按所在站点。"""
    return await stations_svc.batteries(session, principal, station_id=station_id,
                                        status=status, model_code=model_code, q=q,
                                        limit=limit, offset=offset)


@router.get("/batteries/{battery_sn}")
async def get_battery(battery_sn: str, principal: PrincipalDep,
                      session: Db) -> dict[str, Any]:
    return await stations_svc.battery(session, principal, battery_sn)


@router.post("/batteries/{battery_sn}/status")
async def set_battery_status(battery_sn: str, body: BatteryStatusIn, request: Request,
                             principal: PrincipalDep,
                             session: Db) -> dict[str, Any]:
    return await stations_svc.set_battery_status(session, principal, battery_sn, body,
                                                 ip=_ip(request))


# ══ 换电协议与押金 ════════════════════════════════════════════════════════
@router.get("/leases")
async def list_leases(principal: PrincipalDep, session: Db,
                      status: str | None = Query(default=None),
                      model_code: str | None = Query(default=None),
                      operator_org_id: int | None = Query(default=None),
                      limit: int = Query(default=50, ge=1, le=200),
                      offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """协议翻页（带 `total`）。可见性与 `GET /leases/{id}` 同口径。"""
    return await leases_svc.page(session, principal, status=status, model_code=model_code,
                                 operator_org_id=operator_org_id, limit=limit, offset=offset)


@router.post("/leases", status_code=201)
async def create_lease(body: LeaseIn, request: Request, principal: PrincipalDep,
                       session: Db) -> dict[str, Any]:
    return await leases_svc.create(session, principal, body, ip=_ip(request))


@router.get("/deposits/position")
async def deposit_position(principal: PrincipalDep,
                           session: Db) -> dict[str, Any]:
    return await leases_svc.deposit_position(session, principal)


@router.get("/leases/{lease_id}")
async def get_lease(lease_id: int, principal: PrincipalDep,
                    session: Db) -> dict[str, Any]:
    return await leases_svc.detail(session, principal, lease_id)


@router.post("/leases/{lease_id}/deposit", status_code=201)
async def register_deposit(lease_id: int, body: DepositRegisterIn, request: Request,
                           principal: PrincipalDep,
                           session: Db) -> dict[str, Any]:
    return await leases_svc.register_deposit(session, principal, lease_id, body, ip=_ip(request))


@router.post("/leases/{lease_id}/activate")
async def activate_lease(lease_id: int, request: Request, principal: PrincipalDep,
                         session: Db) -> dict[str, Any]:
    return await leases_svc.activate(session, principal, lease_id, ip=_ip(request))


@router.post("/leases/{lease_id}/terminate")
async def terminate_lease(lease_id: int, body: LeaseTerminateIn, request: Request,
                          principal: PrincipalDep,
                          session: Db) -> dict[str, Any]:
    return await leases_svc.terminate(session, principal, lease_id, body, ip=_ip(request))


# ══ 换电单（站点作业台 / 用户发起）═══════════════════════════════════════
@router.post("/swaps", status_code=201)
async def execute_swap(body: SwapIn, request: Request, principal: PrincipalDep,
                       session: Db,
                       idem: IdempotencyDep = None) -> dict[str, Any]:
    """一次换电：五道闸门 + 四写同事务。**必须带 Idempotency-Key**（弱网重试防重复收费）。"""
    return await swaps_svc.execute(session, principal, body, ip=_ip(request),
                                   idempotency_key=idem)


@router.get("/swaps")
async def list_swaps(principal: PrincipalDep, session: Db,
                     status: str | None = Query(default=None),
                     station_id: int | None = Query(default=None),
                     limit: int = Query(default=50, ge=1, le=200),
                     offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await swaps_svc.page(session, principal, status=status, station_id=station_id,
                                limit=limit, offset=offset)


@router.get("/swaps/rejections")
async def swap_rejections(principal: PrincipalDep, session: Db,
                          station_id: int | None = Query(default=None),
                          days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    """拒单原因码统计（P29：白名单配错、池外电池混入必须看得见）。"""
    return await swaps_svc.rejections(session, principal, station_id=station_id, days=days)


@router.get("/swaps/{swap_id}")
async def get_swap(swap_id: int, principal: PrincipalDep,
                   session: Db) -> dict[str, Any]:
    return await swaps_svc.detail(session, principal, swap_id)
