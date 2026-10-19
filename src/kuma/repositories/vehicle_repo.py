"""车辆 / T-Box / SIM / 订阅 的查询与写入（scope 过滤在此层生效）。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Select, func, or_, select

from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.core import Device, Sim, Subscription, SubscriptionPlan, Vehicle, VehicleType
from .base import count_of, vehicle_scope


class VehicleRepo:
    # 方法不叫 list：类里有个叫 list 的成员会让同类里的 `-> list[...]` 注解解析成那个方法（mypy valid-type）
    def page(self, principal: Principal, *, q: str | None = None,
             status: str | None = None, type_code: str | None = None,
             region_id: str | None = None, page: PageParams | None = None) -> Select:
        stmt = select(Vehicle).where(Vehicle.id.in_(vehicle_scope(principal)))
        if status:
            stmt = stmt.where(Vehicle.status == status)
        if type_code:
            stmt = stmt.where(Vehicle.type_code == type_code)
        if region_id:
            stmt = stmt.where(Vehicle.region_id == region_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Vehicle.plate.ilike(like), Vehicle.vin.ilike(like)))
        p = page or PageParams()
        return stmt.order_by(Vehicle.id.desc()).limit(p.limit).offset(p.offset)

    async def count(self, session, principal: Principal, **kw) -> int:
        stmt = select(Vehicle).where(Vehicle.id.in_(vehicle_scope(principal)))
        if kw.get("status"):
            stmt = stmt.where(Vehicle.status == kw["status"])
        if kw.get("region_id"):
            stmt = stmt.where(Vehicle.region_id == kw["region_id"])
        return await count_of(session, stmt)

    async def get_by_vin(self, session, vin: str) -> Vehicle | None:
        return (await session.execute(select(Vehicle).where(Vehicle.vin == vin))).scalar_one_or_none()

    async def get_by_id(self, session, vehicle_id: int) -> Vehicle | None:
        return await session.get(Vehicle, vehicle_id)

    async def devices_for_vin(self, session, vin: str) -> Sequence[Device]:
        return (await session.execute(select(Device).where(Device.vin == vin))).scalars().all()

    async def sim_for_device(self, session, device_id: int) -> Sequence[Sim]:
        return (await session.execute(select(Sim).where(Sim.device_id == device_id))).scalars().all()

    async def active_subscription(self, session, vin: str) -> Subscription | None:
        return (await session.execute(
            select(Subscription).where(Subscription.vin == vin, Subscription.status == "active")
        )).scalar_one_or_none()

    async def plans(self, session, region_id: str | None = None) -> Sequence[SubscriptionPlan]:
        stmt = select(SubscriptionPlan).where(SubscriptionPlan.status == "active")
        if region_id:
            stmt = stmt.where(SubscriptionPlan.region_id == region_id)
        return (await session.execute(stmt.order_by(SubscriptionPlan.id))).scalars().all()

    async def types(self, session, region_id: str | None = None) -> Sequence[VehicleType]:
        stmt = select(VehicleType).where(VehicleType.status == "active")
        if region_id:
            stmt = stmt.where(VehicleType.regions.contains([region_id]))
        return (await session.execute(stmt.order_by(VehicleType.type_code))).scalars().all()

    async def types_page(self, session, *, region_id: str | None = None,
                         product_line: str | None = None, category: str | None = None,
                         q: str | None = None, page: PageParams | None = None
                         ) -> tuple[Sequence[VehicleType], int]:
        """公开目录用（portal 车型参数页）：只查 active，返回 (行, 总数)。"""
        stmt = select(VehicleType).where(VehicleType.status == "active")
        if region_id:
            stmt = stmt.where(VehicleType.regions.contains([region_id]))
        if product_line:
            stmt = stmt.where(VehicleType.product_line == product_line)
        if category:
            stmt = stmt.where(VehicleType.category == category)
        if q:
            stmt = stmt.where(VehicleType.name.ilike(f"%{q}%"))
        total = await count_of(session, stmt)
        p = page or PageParams()
        rows = (await session.execute(
            stmt.order_by(VehicleType.type_code).limit(p.limit).offset(p.offset)
        )).scalars().all()
        return rows, total

    async def type_by_code(self, session, type_code: str) -> VehicleType | None:
        return (await session.execute(
            select(VehicleType).where(VehicleType.type_code == type_code,
                                      VehicleType.status == "active")
        )).scalar_one_or_none()

    async def status_breakdown(self, session, principal: Principal) -> list[dict]:
        """看板用：按状态分组计数（只统计 scope 内车辆）。"""
        stmt = (
            select(Vehicle.status, func.count().label("n"))
            .where(Vehicle.id.in_(vehicle_scope(principal)))
            .group_by(Vehicle.status)
            .order_by(func.count().desc())
        )
        return [dict(status=r.status, n=r.n) for r in (await session.execute(stmt)).all()]
