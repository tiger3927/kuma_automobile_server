"""型号字典、价目/押金规则与业务线申报（M15，`Task-Plan.md` §16.2 / §16.5 / §16.7 / §16.10）。

**闸门前置到字典层**：型号必填项不全就不允许 `active`（DDL 里有同名 CHECK），
不靠站点现场判断。`retired` 只停止新增，**存量电池继续可换**——
否则"平台改了个字典状态"等于让用户手上的电池当场作废。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..models.core import Org, OrgBusiness
from ..repositories.energy_repo import EnergyRepo
from ..schemas.energy import (
    BatteryModelIn,
    BatteryModelPatch,
    BatteryModelStatusIn,
    DepositRuleIn,
    OrgBusinessIn,
    RuleApproveIn,
    SwapPriceIn,
)

# 置 active 的必填项（与 DDL 的 ck_battery_model_active_complete 同口径，
# 这里先给可读错误，DDL 兜底防绕过）
ACTIVE_REQUIRED = ("spec_doc_media_id", "mounting_drawing_media_id")


class BatteryModelService:
    repo = EnergyRepo()

    # ══ 型号字典 ═══════════════════════════════════════════════════════════
    async def create(self, session: AsyncSession, principal: Principal, body: BatteryModelIn, *,
                     ip: str | None = None) -> dict[str, Any]:
        """录入型号（一律落 `draft`，审核后才 active）。字段由规划定义，厂家只填值。"""
        principal.require("battery_model", "write")
        if await self.repo.get_model(session, body.model_code) is not None:
            raise Conflict("型号编码已存在（编码规则 KM-<化学>-<度数×10>，不可复用）",
                           code="model_code_exists")
        if body.kwh_accept_min is not None and body.kwh_accept_min > body.kwh_nominal:
            raise ValidationFailed("kwh_accept_min 不能高于 kwh_nominal",
                                   code="accept_min_above_nominal")
        await self._require_type_codes(session, body.compatible_types)

        data = body.model_dump()
        data["kwh_accept_min"] = (body.kwh_accept_min
                                  if body.kwh_accept_min is not None
                                  else round(body.kwh_nominal * 0.9, 2))
        # needs_handling_aid 由重量推导（§16.2 阈值 12 kg），不让录入方自己填
        data["needs_handling_aid"] = float(body.weight_kg) >= 12
        data["telemetry_readable"] = False      # v1 恒 false：不读电池遥测（§16.9）
        row = await self.repo.insert_model(session, m=data)
        await write_audit(session, actor=principal, action="battery_model.create",
                          resource_type="battery_model", resource_id=row["model_code"],
                          after={"model_code": row["model_code"], "kwh_nominal": body.kwh_nominal,
                                 "status": "draft"}, ip=ip)
        return row

    async def patch(self, session: AsyncSession, principal: Principal, model_code: str,
                    body: BatteryModelPatch, *, ip: str | None = None) -> dict[str, Any]:
        principal.require("battery_model", "write")
        if await self.repo.get_model(session, model_code) is None:
            raise NotFound("battery_model_not_found")
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise ValidationFailed("没有要改的字段", code="empty_patch")
        if "compatible_types" in fields:
            await self._require_type_codes(session, fields["compatible_types"] or [])
        row = await self.repo.patch_model(session, model_code, **fields)
        assert row is not None
        await write_audit(session, actor=principal, action="battery_model.update",
                          resource_type="battery_model", resource_id=model_code,
                          before=None, after=fields, ip=ip)
        return row

    async def set_status(self, session: AsyncSession, principal: Principal, model_code: str,
                         body: BatteryModelStatusIn, *, ip: str | None = None) -> dict[str, Any]:
        """`draft → active → retired`：置 active 前必须规格完整（缺字段就拒，不靠现场判断）。"""
        principal.require("battery_model", "write")
        model = await self.repo.get_model(session, model_code)
        if model is None:
            raise NotFound("battery_model_not_found")
        if body.status == "active":
            missing = [c for c in ACTIVE_REQUIRED if model.get(c) is None]
            if not (model["compatible_types"] or []):
                missing.append("compatible_types")
            if missing:
                raise InvalidState(f"型号规格不完整，缺 {missing}，不能置 active",
                                   code="model_incomplete")
        elif body.status == "retired" and model["status"] != "active":
            raise InvalidState("只有 active 型号能退役", code="model_state")
        row = await self.repo.set_model_status(session, model_code, body.status,
                                               principal.account_id)
        assert row is not None
        await write_audit(session, actor=principal, action="battery_model.status",
                          resource_type="battery_model", resource_id=model_code,
                          before={"status": model["status"]},
                          after={"status": body.status, "note": body.note}, ip=ip)
        await emit(session, aggregate_type="battery_model", aggregate_id=model_code,
                   event_type=f"status_{body.status}",
                   payload={"model_code": model_code, "from": model["status"],
                            "to": body.status})
        return row

    async def detail(self, session: AsyncSession, principal: Principal,
                     model_code: str) -> dict[str, Any]:
        principal.require("battery_model", "read")
        row = await self.repo.get_model(session, model_code)
        if row is None:
            raise NotFound("battery_model_not_found")
        return row

    async def page(self, session: AsyncSession, principal: Principal, *, status: str | None = None,
                   limit: int = 100, offset: int = 0) -> dict[str, Any]:
        principal.require("battery_model", "read")
        return {"items": await self.repo.list_models(session, status=status, limit=limit,
                                                     offset=offset),
                "limit": limit, "offset": offset}

    # ══ 价目与押金规则（运营商申报 → 平台审核 → active）════════════════════
    async def propose_price(self, session: AsyncSession, principal: Principal, body: SwapPriceIn,
                            *, ip: str | None = None) -> dict[str, Any]:
        principal.require("deposit", "write")     # 价目与押金同属"运营商申报"权限组
        org_id = self._operator_org(principal, body.station_org_id)
        if await self.repo.get_model(session, body.model_code) is None:
            raise NotFound("battery_model_not_found")
        row = await self.repo.insert_price(session, model_code=body.model_code,
                                           region_id=body.region_id, station_org_id=org_id,
                                           price=body.price, currency=body.currency,
                                           min_soc_pct=body.min_soc_pct, version=body.version,
                                           effective_from=body.effective_from,
                                           effective_to=body.effective_to,
                                           proposed_by=principal.account_id)
        await write_audit(session, actor=principal, action="swap_price.propose",
                          resource_type="swap_price", resource_id=row["id"], after=row, ip=ip)
        return row

    async def propose_deposit_rule(self, session: AsyncSession, principal: Principal,
                                   body: DepositRuleIn, *, ip: str | None = None) -> dict[str, Any]:
        principal.require("deposit", "write")
        self._operator_org(principal, None)
        if await self.repo.get_model(session, body.model_code) is None:
            raise NotFound("battery_model_not_found")
        row = await self.repo.insert_rule(session, model_code=body.model_code,
                                          region_id=body.region_id, amount=body.amount,
                                          currency=body.currency,
                                          waiver_min_kyc=body.waiver_min_kyc,
                                          version=body.version, effective_from=body.effective_from,
                                          effective_to=body.effective_to,
                                          proposed_by=principal.account_id)
        await write_audit(session, actor=principal, action="deposit_rule.propose",
                          resource_type="deposit_rule", resource_id=row["id"], after=row, ip=ip)
        return row

    async def approve_rule(self, session: AsyncSession, principal: Principal, table: str,
                           rule_id: int, body: RuleApproveIn, *,
                           ip: str | None = None) -> dict[str, Any]:
        """只有平台能审核生效（运营商不能自己给自己定价）。"""
        if table not in ("swap_price", "deposit_rule"):
            raise ValidationFailed("table 只能是 swap_price / deposit_rule", code="bad_table")
        if not principal.is_platform:
            raise Forbidden("rule_approval_requires_platform")
        principal.require("battery_model" if table == "swap_price" else "deposit", "write")
        row = await self.repo.approve_rule(session, table=table, rule_id=rule_id,
                                           approve=body.approve, approver=principal.account_id)
        if row is None:
            raise NotFound("rule_not_found")
        await write_audit(session, actor=principal, action=f"{table}.approve",
                          resource_type=table, resource_id=rule_id,
                          before=None, after={"status": row["status"], "note": body.note}, ip=ip)
        return row

    # ══ 业务线与牌照（换电 scope 的唯一入口）══════════════════════════════
    async def declare_business(self, session: AsyncSession, principal: Principal,
                               body: OrgBusinessIn, *, ip: str | None = None) -> dict[str, Any]:
        """申报业务线 + 牌照：没有这条记录，该 org 的员工拿不到任何换电 scope。

        平台可直接登记；组织自己申报需 primary_admin（与 `org_service` 同口径）。
        """
        org = (await session.execute(select(Org).where(Org.id == body.org_id))
               ).scalar_one_or_none()
        if org is None:
            raise NotFound("org_not_found")
        if not principal.is_platform:
            ref = next((o for o in principal.orgs if o.org_id == body.org_id), None)
            if ref is None or ref.role != "primary_admin":
                raise Forbidden("只有该组织的 primary_admin 或平台能申报业务线")
        existing = (await session.execute(
            select(OrgBusiness).where(OrgBusiness.org_id == body.org_id,
                                      OrgBusiness.line == body.line))).scalar_one_or_none()
        if existing is not None:
            raise Conflict("该组织已申报这条业务线", code="business_line_exists")
        session.add(OrgBusiness(org_id=body.org_id, line=body.line, license_no=body.license_no,
                                license_expiry=body.license_expiry,
                                approved_by=principal.account_id))
        await session.flush()
        await write_audit(session, actor=principal, action="org_business.declare",
                          resource_type="org_business", resource_id=body.org_id,
                          after={"line": body.line, "license_no": body.license_no,
                                 "license_expiry": str(body.license_expiry or "")}, ip=ip)
        return {"org_id": body.org_id, "line": body.line, "status": "active"}

    # ══ 内部 ═══════════════════════════════════════════════════════════════
    def _operator_org(self, principal: Principal, org_id: int | None) -> int:
        swap_orgs = principal.org_ids_for_line("swap_operator")
        if principal.is_platform:
            if org_id is None:
                raise ValidationFailed("平台代申报必须指定 station_org_id",
                                       code="station_org_required")
            return org_id
        if org_id is not None and org_id not in swap_orgs:
            raise Forbidden("只能挂到自己有换电业务线的组织")
        if not swap_orgs:
            raise Forbidden("该账号没有 swap_operator 业务线，不能申报换电价目或押金规则")
        return org_id or swap_orgs[0]

    async def _require_type_codes(self, session: AsyncSession, codes: list[str]) -> None:
        """兼容车型必须真实存在（否则"双向声明"会指向不存在的 type_code）。"""
        if not codes:
            return
        from ..models.core import VehicleType

        known = set((await session.execute(
            select(VehicleType.type_code).where(VehicleType.type_code.in_(codes)))
        ).scalars().all())
        unknown = [c for c in codes if c not in known]
        if unknown:
            raise ValidationFailed(f"这些车型不存在：{unknown}", code="unknown_type_codes")
