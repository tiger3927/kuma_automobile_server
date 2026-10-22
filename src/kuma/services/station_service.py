"""换电站、准入型号白名单与电池入池（M15，`Task-Plan.md` §16.3 / §16.4）。

**白名单是站点的核心配置**：柜位、充电架、计价范围全按型号选型。
改白名单必须写审计（谁、何时、加减了哪个型号）——否则无法解释"为什么昨天能换今天不能"。
删型号只影响**新入池**，已在站内的电池仍可被换出（不追溯，§16.2 状态规则）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..repositories.energy_repo import EnergyRepo
from ..schemas.energy import (
    AllowedModelsIn,
    BatteryIn,
    BatteryStatusIn,
    NearbyQuery,
    StationIn,
    StationPatch,
)

# 库存三个计数只覆盖这三种状态；其余状态（in_stock/returned/leased/…）不计数
STOCK_STATUSES = ("ready", "charging", "faulty")
MANUAL_TARGETS = ("in_stock", "charging", "ready", "returned", "faulty", "repair",
                  "retired", "lost", "stolen")


def _stock_delta(from_status: str | None, to_status: str) -> tuple[int, int, int]:
    """把状态变化折算成 (ready, charging, faulty) 的增减。

    只在这里算一次：任何别处手工调库存都会让 `station_stock` 与 `battery` 漂移（P26）。
    """
    def vec(status: str | None) -> tuple[int, int, int]:
        if status in STOCK_STATUSES:
            return (1 if status == "ready" else 0,
                    1 if status == "charging" else 0,
                    1 if status == "faulty" else 0)
        return (0, 0, 0)

    a, b = vec(from_status), vec(to_status)
    return (b[0] - a[0], b[1] - a[1], b[2] - a[2])


class StationService:
    repo = EnergyRepo()

    # ══ 站点 ═══════════════════════════════════════════════════════════════
    async def create(self, session: AsyncSession, principal: Principal, body: StationIn, *,
                     ip: str | None = None) -> dict[str, Any]:
        principal.require("station", "write")
        org_id = self._resolve_org(principal, body.org_id)
        await self._require_models(session, body.allowed_models)
        if await self.repo.get_station(session, code=body.code) is not None:
            raise Conflict("站点编码已存在", code="station_code_exists")

        models = await self._models_of(session, body.allowed_models)
        needs_aid = any(float(m["weight_kg"]) >= 12 for m in models)   # §16.2 阈值
        station = await self.repo.insert_station(
            session, code=body.code, name=body.name, org_id=org_id, lng=body.lng, lat=body.lat,
            address=body.address, region_id=body.region_id,
            business_hours=body.business_hours, allowed_models=body.allowed_models,
            bay_count=body.bay_count, needs_handling_aid=needs_aid)
        await write_audit(session, actor=principal, action="station.create",
                          resource_type="station", resource_id=station["id"],
                          after={"code": body.code, "name": body.name, "org_id": org_id,
                                 "allowed_models": body.allowed_models}, ip=ip)
        await emit(session, aggregate_type="station", aggregate_id=station["id"],
                   event_type="created",
                   payload={"code": body.code, "org_id": org_id,
                            "allowed_models": body.allowed_models})
        return station

    async def patch(self, session: AsyncSession, principal: Principal, station_id: int,
                    body: StationPatch, *, ip: str | None = None) -> dict[str, Any]:
        station = await self._get_visible(session, principal, station_id)
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise ValidationFailed("没有要改的字段", code="empty_patch")
        row = await self.repo.patch_station(session, station_id, **fields)
        assert row is not None
        await write_audit(session, actor=principal, action="station.update",
                          resource_type="station", resource_id=station_id,
                          before={k: station.get(k) for k in fields}, after=fields, ip=ip)
        return row

    async def set_allowed_models(self, session: AsyncSession, principal: Principal,
                                 station_id: int, body: AllowedModelsIn, *,
                                 ip: str | None = None) -> dict[str, Any]:
        """改准入白名单（**必须留审计**：这是"为什么这台电池进不来"的唯一解释来源）。"""
        station = await self._get_visible(session, principal, station_id)
        principal.require("station", "write")
        current = list(station["allowed_models"] or [])
        remove = set(body.remove)
        add = [m for m in body.add if m not in current]
        if not add and not remove:
            raise ValidationFailed("add / remove 至少给一个", code="empty_change")
        await self._require_models(session, add)
        unknown_remove = [m for m in remove if m not in current]
        if unknown_remove:
            raise ValidationFailed(f"这些型号本来就不在白名单：{unknown_remove}",
                                   code="model_not_in_whitelist")
        new_models = [m for m in current if m not in remove] + add
        row = await self.repo.set_allowed_models(session, station_id, new_models)
        assert row is not None
        await write_audit(session, actor=principal, action="station.allowed_models",
                          resource_type="station", resource_id=station_id,
                          before={"allowed_models": current},
                          after={"allowed_models": new_models, "added": add,
                                 "removed": sorted(remove)}, ip=ip)
        await emit(session, aggregate_type="station", aggregate_id=station_id,
                   event_type="allowed_models_changed",
                   payload={"added": add, "removed": sorted(remove)})
        return row

    async def detail(self, session: AsyncSession, principal: Principal,
                     station_id: int) -> dict[str, Any]:
        station = await self._get_visible(session, principal, station_id)
        return {"station": station, "stock": await self.repo.stock(session, station_id)}

    async def page(self, session: AsyncSession, principal: Principal, *, status: str | None = None,
                   limit: int = 50, offset: int = 0) -> dict[str, Any]:
        principal.require("station", "read")
        if principal.is_platform or principal.is_regulator:
            orgs: list[int] | None = None
        else:
            orgs = principal.org_ids_for_line("swap_operator")
            if not orgs:
                return {"items": [], "limit": limit, "offset": offset}
        return {"items": await self.repo.list_stations(session, org_ids=orgs, status=status,
                                                       limit=limit, offset=offset),
                "limit": limit, "offset": offset}

    async def nearby(self, session: AsyncSession, principal: Principal,
                     q: NearbyQuery) -> dict[str, Any]:
        """用户端/门户的唯一出口：距离 + 该型号可用电池数 + 营业时间（价格与库存必须公开）。"""
        principal.require("station", "read")
        return {"items": await self.repo.nearby(session, lng=q.lng, lat=q.lat,
                                                radius_km=q.radius_km,
                                                model_code=q.model_code, limit=q.limit)}

    # ══ 电池入池与状态 ═════════════════════════════════════════════════════
    async def register_battery(self, session: AsyncSession, principal: Principal, body: BatteryIn,
                               *, ip: str | None = None) -> dict[str, Any]:
        """入池三前提（§16.3）：型号非 draft + 在该站白名单内 + 实装容量达验收下限。

        池外型号（第三方、未认证旧型号）**不进台账**——收进一块换不出去的电池，
        SN 台账、押金赔偿、站点库存会三处同时失真（P29）。
        """
        principal.require("battery", "write")
        if await self.repo.get_battery(session, body.battery_sn) is not None:
            raise Conflict("该 SN 已在台账", code="battery_exists")
        model = await self.repo.get_model(session, body.model_code)
        if model is None:
            raise NotFound("battery_model_not_found")
        if model["status"] == "draft":
            raise InvalidState("型号规格不完整或未审核，不能建电池台账", code="model_not_active")
        if body.station_id is None:
            raise ValidationFailed("入池必须指定 station_id", code="station_id_required")
        station = await self._get_visible(session, principal, body.station_id)
        if body.model_code not in (station["allowed_models"] or []):
            raise InvalidState(
                f"型号 {body.model_code} 不在站点 {station['code']} 的准入白名单内，拒收",
                code="model_not_allowed")
        if body.kwh_actual is None:
            raise ValidationFailed("入池必须登记实测容量 kwh_actual（验收）",
                                   code="kwh_actual_required")
        accept_min = float(model["kwh_accept_min"])
        if float(body.kwh_actual) < accept_min:
            raise InvalidState(
                f"实测 {body.kwh_actual} kWh 低于该型号验收下限 {accept_min} kWh，拒绝入池",
                code="battery_below_accept")

        owner_org_id = self._resolve_org(principal, body.owner_org_id)
        battery = await self.repo.insert_battery(
            session, battery_sn=body.battery_sn, model_code=body.model_code,
            owner_org_id=owner_org_id, batch_no=body.batch_no, kwh_actual=body.kwh_actual,
            accept_media_id=body.accept_media_id, station_id=body.station_id, status="in_stock")
        await self.repo.append_flow(session, battery_sn=body.battery_sn, from_status=None,
                                    to_status="in_stock", station_id=body.station_id, vin=None,
                                    swap_order_id=None, actor_account_id=principal.account_id,
                                    actor_role=principal.active_identity, note=body.note,
                                    media_id=body.accept_media_id)
        await write_audit(session, actor=principal, action="battery.register",
                          resource_type="battery", resource_id=battery["battery_sn"],
                          after={"model_code": body.model_code, "owner_org_id": owner_org_id,
                                 "station_id": body.station_id, "kwh_actual": body.kwh_actual},
                          ip=ip)
        return battery

    async def set_battery_status(self, session: AsyncSession, principal: Principal,
                                 battery_sn: str, body: BatteryStatusIn, *,
                                 ip: str | None = None) -> dict[str, Any]:
        """人工改状态（故障隔离 / 退役 / 丢失 / 入站检测完成转 ready）。换电引起的变化不走这里。"""
        principal.require("battery", "write")
        if body.to_status not in MANUAL_TARGETS:
            raise ValidationFailed(f"不支持的目标状态 {body.to_status}", code="bad_target")
        battery = await self.repo.get_battery(session, battery_sn)
        if battery is None:
            raise NotFound("battery_not_found")
        self._assert_battery_visible(principal, battery)
        if battery["status"] == "leased":
            raise InvalidState("电池在车上，先在站点完成换电或回收，不能直接改状态",
                               code="battery_on_vehicle")
        if body.to_status in STOCK_STATUSES and battery["station_id"] is None and \
                body.station_id is None:
            raise ValidationFailed("置 ready/charging/faulty 必须有所在站点",
                                   code="station_id_required")
        if body.to_status == "ready" and body.soc is None and battery["soc"] is None:
            raise ValidationFailed("置 ready 前必须登记 SOC（站点后台更新，不来自遥测）",
                                   code="soc_required")

        station_id = battery["station_id"] if body.station_id is None else body.station_id
        if body.station_id is not None:
            await self._get_visible(session, principal, body.station_id)

        row = await self.repo.set_battery_state(session, battery_sn, status=body.to_status,
                                                station_id=station_id, soc=body.soc)
        assert row is not None
        rd, cd, fd = _stock_delta(battery["status"], body.to_status)
        if (rd or cd or fd) and station_id is not None:
            await self.repo.stock_adjust(session, station_id=station_id,
                                         model_code=battery["model_code"],
                                         ready_delta=rd, charging_delta=cd, faulty_delta=fd)
        await self.repo.append_flow(session, battery_sn=battery_sn,
                                    from_status=battery["status"], to_status=body.to_status,
                                    station_id=station_id, vin=None, swap_order_id=None,
                                    actor_account_id=principal.account_id,
                                    actor_role=principal.active_identity, note=body.note,
                                    media_id=body.media_id)
        await write_audit(session, actor=principal, action="battery.status",
                          resource_type="battery", resource_id=battery_sn,
                          before={"status": battery["status"], "station_id": battery["station_id"]},
                          after={"status": body.to_status, "station_id": station_id,
                                 "soc": body.soc, "note": body.note}, ip=ip)
        return row

    async def battery(self, session: AsyncSession, principal: Principal,
                      battery_sn: str) -> dict[str, Any]:
        principal.require("battery", "read")
        battery = await self.repo.get_battery(session, battery_sn)
        if battery is None:
            raise NotFound("battery_not_found")
        self._assert_battery_visible(principal, battery)
        binding = await self.repo.active_binding(session, battery_sn=battery_sn)
        return {"battery": battery, "on_vin": (binding or {}).get("vin"),
                "flows": await self.repo.flows(session, battery_sn)}

    async def stock(self, session: AsyncSession, principal: Principal,
                    station_id: int) -> dict[str, Any]:
        """库存 + 对账：`station_stock` 是对外唯一来源，重算值只用来**发现漂移并告警**，不自动改。"""
        await self._get_visible(session, principal, station_id)
        stock = await self.repo.stock(session, station_id)
        recomputed = await self.repo.stock_recompute(session, station_id)
        by_model = {r["model_code"]: r for r in stock}
        drift = []
        for r in recomputed:
            cur = by_model.get(r["model_code"], {})
            if (int(cur.get("ready_count", 0)) != int(r["ready"])
                    or int(cur.get("charging_count", 0)) != int(r["charging"])
                    or int(cur.get("faulty_count", 0)) != int(r["faulty"])):
                drift.append({"model_code": r["model_code"], "stock": cur, "recomputed": r})
        return {"station_id": station_id, "stock": stock, "recomputed": recomputed,
                "drift": drift,
                "note": "对外只读 stock；drift 非空说明某条路径漏写库存，按告警处理，不自动改"}

    # ══ 内部 ═══════════════════════════════════════════════════════════════
    async def _get_visible(self, session: AsyncSession, principal: Principal,
                           station_id: int) -> dict[str, Any]:
        principal.require("station", "read")
        station = await self.repo.get_station(session, station_id=station_id)
        if station is None:
            raise NotFound("station_not_found")
        if not (principal.is_platform or principal.is_regulator) \
                and station["org_id"] not in principal.org_ids_for_line("swap_operator"):
            raise Forbidden("station_out_of_scope")
        return station

    def _resolve_org(self, principal: Principal, org_id: int | None) -> int:
        """站点/电池的归属运营商：平台必须显式指定，运营商只能用自己的 org。"""
        swap_orgs = principal.org_ids_for_line("swap_operator")
        if principal.is_platform or principal.is_regulator:
            if org_id is None:
                raise ValidationFailed("平台建站点/电池必须指定 org_id", code="org_id_required")
            return org_id
        if org_id is not None and org_id not in swap_orgs:
            raise Forbidden("只能挂到自己有换电业务线的组织")
        if not swap_orgs:
            raise Forbidden("该账号没有任何带 swap_operator 业务线的组织，不能建换电站或入池电池")
        return org_id or swap_orgs[0]

    def _assert_battery_visible(self, principal: Principal, battery: dict[str, Any]) -> None:
        if principal.is_platform or principal.is_regulator:
            return
        if battery["owner_org_id"] in principal.org_ids_for_line("swap_operator"):
            return
        raise Forbidden("battery_out_of_scope")

    async def _require_models(self, session: AsyncSession, codes: list[str]) -> None:
        for code in codes:
            m = await self.repo.get_model(session, code)
            if m is None:
                raise NotFound("battery_model_not_found")
            if m["status"] != "active":
                raise InvalidState(f"型号 {code} 未生效，不能进准入白名单",
                                   code="model_not_active")

    async def _models_of(self, session: AsyncSession, codes: list[str]) -> list[dict[str, Any]]:
        out = []
        for code in codes:
            m = await self.repo.get_model(session, code)
            if m is not None:
                out.append(m)
        return out
