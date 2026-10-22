"""repository 基座：**scope 过滤器的唯一落地点**。

Task-Plan.md §2.2 约束 2 / §10-8：所有列表查询必须经这里，api 层禁止手写 `WHERE org_id = :id`；
越权返回空集而不是"他人数据计数"。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import Forbidden
from ..core.scope import Principal
from ..models.biz import Trip
from ..models.core import Vehicle
from ..models.energy import Station, SwapOrder
from ..models.mobility import FleetContract


def vehicle_scope(principal: Principal) -> Select:
    """返回"该 principal 可见的车辆"子查询（列：core.vehicle.id）。"""
    stmt = select(Vehicle.id)
    if principal.is_platform or principal.is_regulator:
        return stmt

    conds = []
    identities = set(principal.identities)

    if "owner" in identities:
        conds.append(Vehicle.owner_account_id == principal.account_id)
    if "dealer_staff" in identities:
        orgs = principal.org_ids("dealer")
        if orgs:
            conds.append(or_(Vehicle.cur_dealer_org_id.in_(orgs), Vehicle.owner_org_id.in_(orgs)))
    if "operator_staff" in identities:
        orgs = principal.org_ids("operator")
        if orgs:
            conds.append(or_(
                Vehicle.owner_org_id.in_(orgs),
                Vehicle.id.in_(
                    select(FleetContract.vehicle_id).where(
                        FleetContract.operator_org_id.in_(orgs),
                        FleetContract.status == "active")
                ),
            ))
    if "driver" in identities:
        conds.append(Vehicle.id.in_(
            select(FleetContract.vehicle_id).where(
                FleetContract.driver_account_id == principal.account_id,
                FleetContract.status == "active")
        ))

    if not conds:
        # 无任何可见条件：返回恒假子查询（空集），而不是放行全表
        return stmt.where(Vehicle.id.is_(None))
    return stmt.where(or_(*conds))


def assert_vehicle_visible(principal: Principal, vehicle: Vehicle) -> None:
    """单对象读写的归属校验（列表用 scope 过滤，单条用这里）。"""
    if principal.is_platform or principal.is_regulator:
        return
    if "owner" in principal.identities and vehicle.owner_account_id == principal.account_id:
        return
    if "dealer_staff" in principal.identities and vehicle.cur_dealer_org_id in principal.org_ids("dealer"):
        return
    orgs = principal.org_ids("operator")
    if "operator_staff" in principal.identities and vehicle.owner_org_id in orgs:
        return
    raise Forbidden("vehicle_out_of_scope")


def station_scope(principal: Principal) -> Select:
    """换电站可见范围（M15）：按**业务线** `swap_operator` 判定，不看 `org_type`。

    为什么不用 `org_ids("operator")`：一家公司可能既是网约车运营商又是换电站运营商，
    `org_type` 单值记不下；反过来，只有 `org_type=operator` 但没申报换电业务线的组织
    **拿不到换电 scope**（业务线与牌照是线下准入的线上事实，`Task-Plan.md` §16.10）。
    """
    stmt = select(Station.id)
    if principal.is_platform or principal.is_regulator:
        return stmt
    orgs = principal.org_ids_for_line("swap_operator")
    if not orgs:
        return stmt.where(Station.id.is_(None))     # 恒假：不是"放行全表"
    return stmt.where(Station.org_id.in_(orgs))


def assert_station_visible(principal: Principal, station: Station) -> None:
    """站点单对象读写归属（列表用 `station_scope`，单条用这里）。"""
    if principal.is_platform or principal.is_regulator:
        return
    if station.org_id in principal.org_ids_for_line("swap_operator"):
        return
    raise Forbidden("station_out_of_scope")


def swap_scope(principal: Principal) -> Select:
    """换电单可见范围：运营商看本站本运营商，用户看自己发起的，平台/监管全量。"""
    stmt = select(SwapOrder.id)
    if principal.is_platform or principal.is_regulator:
        return stmt
    conds: list[Any] = []
    orgs = principal.org_ids_for_line("swap_operator")
    if orgs:
        conds.append(SwapOrder.operator_org_id.in_(orgs))
    if {"consumer", "owner", "driver"} & set(principal.identities):
        conds.append(SwapOrder.account_id == principal.account_id)
    if not conds:
        return stmt.where(SwapOrder.id.is_(None))
    return stmt.where(or_(*conds))


def trip_scope_clause(principal: Principal):
    """行程可见性：车主按**当时归属**（过户后原主保留历史 trip，Task-Plan.md §4.4）。"""
    if principal.is_platform or principal.is_regulator:
        return None
    conds = []
    if "owner" in principal.identities or "consumer" in principal.identities:
        conds.append(Trip.owner_account_id == principal.account_id)
    if "operator_staff" in principal.identities:
        orgs = principal.org_ids("operator")
        if orgs:
            conds.append(Trip.operator_org_id.in_(orgs))
    if "driver" in principal.identities:
        conds.append(Trip.driver_account_id == principal.account_id)
    if "dealer_staff" in principal.identities:
        orgs = principal.org_ids("dealer")
        if orgs:
            conds.append(Trip.owner_org_id.in_(orgs))
    if not conds:
        return Trip.id.is_(None)
    return or_(*conds)


async def count_of(session: AsyncSession, stmt: Select) -> int:
    from sqlalchemy import func

    sub = stmt.order_by(None).subquery()
    return int((await session.execute(select(func.count()).select_from(sub))).scalar_one())
