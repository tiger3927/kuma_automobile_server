"""换电协议与押金（M15，`Task-Plan.md` §16.6 / §16.7）。

三条不可绕过的口径：

1. **押金是协议的生效前置**：`draft →（押金 held 或免押豁免）→ active`。
   未置 `active` 的协议不能换电（`swap_service` 闸门 ④）。
2. **押金挂在协议上，不挂在电池上**：池化流通会换电池，挂电池会错位；
   协议存 `deposit_amount_snapshot`（签约时档位），**改押金规则不追溯已签协议**。
3. **平台全程不经手钱**（P28 合规立场）：押金由运营商**线下收**，本服务只做
   "登记凭证 + 状态流转"，不生成支付单、不进 `fin` 收入与结算，
   退押也是登记线下退汇凭证。免押只是押金为 0，**协议照签**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..repositories.energy_repo import EnergyRepo
from ..schemas.energy import DepositRegisterIn, LeaseIn, LeaseTerminateIn


class LeaseService:
    repo = EnergyRepo()

    # ══ 协议 ═══════════════════════════════════════════════════════════════
    async def create(self, session: AsyncSession, principal: Principal, body: LeaseIn, *,
                     ip: str | None = None) -> dict[str, Any]:
        """签换电协议（默认持有人 = 当前账号；运营商可为自己的资产车签）。"""
        principal.require("swap_lease", "write")

        model = await self.repo.get_model(session, body.model_code)
        if model is None:
            raise NotFound("battery_model_not_found")
        if model["status"] != "active":
            raise InvalidState("型号未生效（规格不完整或未审核），不能签协议",
                               code="model_not_active")

        operator_org_id, holder_type, holder_account_id, holder_org_id, region_id = \
            self._resolve_parties(principal, body)

        existing = await self.repo.find_lease(
            session, account_id=holder_account_id, org_id=holder_org_id,
            model_code=body.model_code, operator_org_id=operator_org_id)
        if existing is not None:
            raise Conflict("同一持有人对同一运营商的该型号已有生效协议", code="lease_exists")

        # 押金按地区分档：没显式给地区时，按**运营商所在国家**取（§16.7 三国各配一行）
        if region_id is None:
            region_id = await self.repo.org_region(session, operator_org_id)
        rule = await self.repo.effective_deposit_rule(session, model_code=body.model_code,
                                                      region_id=region_id)
        if rule is None:
            raise ValidationFailed(
                f"型号 {body.model_code} 在地区 {region_id or '（全区）'} 没有生效押金规则，"
                "无法签约（押金档位是协议生效前置）", code="deposit_rule_not_configured")

        waived = principal.kyc_level >= int(rule["waiver_min_kyc"])
        lease = await self.repo.insert_lease(
            session, lease_no=self._lease_no(), holder_type=holder_type,
            holder_account_id=holder_account_id, holder_org_id=holder_org_id,
            operator_org_id=operator_org_id, model_code=body.model_code,
            deposit_amount=0 if waived else int(rule["amount"]), currency=rule["currency"],
            deposit_waived=waived, terms_version=body.terms_version)

        if waived:
            # 免押 = 押金为 0，但协议仍必须签（"免押"绝不能变成"免协议"，§16.7）
            activated = await self.repo.set_lease_status(session, lease["id"], "active",
                                                         joined=True)
            assert activated is not None
            lease = activated
        await write_audit(session, actor=principal, action="swap_lease.create",
                          resource_type="swap_lease", resource_id=lease["id"],
                          after={"lease_no": lease["lease_no"], "model_code": body.model_code,
                                 "operator_org_id": operator_org_id,
                                 "deposit_amount": lease["deposit_amount_snapshot"],
                                 "deposit_waived": waived,
                                 "terms_version": body.terms_version}, ip=ip)
        await emit(session, aggregate_type="swap_lease", aggregate_id=lease["id"],
                   event_type="created",
                   payload={"lease_no": lease["lease_no"], "status": lease["status"],
                            "model_code": body.model_code, "operator_org_id": operator_org_id,
                            "deposit_waived": waived})
        return {"lease": lease, "deposit_required": 0 if waived else int(rule["amount"]),
                "deposit_waived": waived, "currency": rule["currency"]}

    async def register_deposit(self, session: AsyncSession, principal: Principal, lease_id: int,
                               body: DepositRegisterIn, *, ip: str | None = None) -> dict[str, Any]:
        """登记"运营商已线下收到押金"——平台不碰钱，只登记凭证（P28）。"""
        lease = await self._require_operator_of(session, principal, lease_id)
        if lease["status"] not in ("draft", "overdue"):
            raise InvalidState(f"协议状态 {lease['status']} 不能登记押金", code="lease_state")
        if lease["deposit_waived"]:
            raise ValidationFailed("免押协议不需要登记押金", code="deposit_waived")
        if await self.repo.deposit_of_lease(session, lease_id, open_only=True) is not None:
            raise Conflict("该协议已有在押押金", code="deposit_exists")

        amount = int(body.amount) if body.amount is not None else int(
            lease["deposit_amount_snapshot"])
        if amount < int(lease["deposit_amount_snapshot"]):
            raise ValidationFailed(
                f"押金不得低于签约档位 {lease['deposit_amount_snapshot']} {lease['currency']}",
                code="deposit_below_snapshot")

        deposit = await self.repo.insert_deposit(
            session, deposit_no=self._deposit_no(), lease_id=lease_id,
            payer_type=lease["holder_type"],
            payer_id=lease["holder_account_id"] or lease["holder_org_id"] or 0,
            operator_org_id=lease["operator_org_id"], amount=amount,
            currency=lease["currency"], status="held",
            collected_voucher_no=body.collected_voucher_no, proof_media_id=body.proof_media_id)
        await write_audit(session, actor=principal, action="deposit.register",
                          resource_type="deposit", resource_id=deposit["id"],
                          after={"deposit_no": deposit["deposit_no"], "lease_id": lease_id,
                                 "amount": amount, "currency": lease["currency"],
                                 "voucher_no": body.collected_voucher_no}, ip=ip)
        await emit(session, aggregate_type="swap_lease", aggregate_id=lease_id,
                   event_type="deposit_held",
                   payload={"amount": amount, "currency": lease["currency"],
                            "deposit_no": deposit["deposit_no"]})
        return {"deposit": deposit, "lease": lease}

    async def activate(self, session: AsyncSession, principal: Principal, lease_id: int, *,
                       ip: str | None = None) -> dict[str, Any]:
        """置 active：押金 held 或免押豁免，二者必居其一（§16.6 状态机）。"""
        lease = await self._require_holder_or_operator(session, principal, lease_id)
        if lease["status"] == "active":
            return {"lease": lease, "already": True}
        if lease["status"] not in ("draft", "overdue"):
            raise InvalidState(f"协议状态 {lease['status']} 不能生效", code="lease_state")
        if not lease["deposit_waived"]:
            deposit = await self.repo.deposit_of_lease(session, lease_id, open_only=True)
            if deposit is None or deposit["status"] != "held":
                raise InvalidState("押金未到账，协议不能生效", code="deposit_missing")
        row = await self.repo.set_lease_status(session, lease_id, "active", joined=True)
        assert row is not None
        await write_audit(session, actor=principal, action="swap_lease.activate",
                          resource_type="swap_lease", resource_id=lease_id,
                          before={"status": lease["status"]}, after={"status": "active"}, ip=ip)
        await emit(session, aggregate_type="swap_lease", aggregate_id=lease_id,
                   event_type="activated",
                   payload={"lease_no": row["lease_no"], "model_code": row["model_code"],
                            "operator_org_id": row["operator_org_id"]})
        return {"lease": row}

    async def terminate(self, session: AsyncSession, principal: Principal, lease_id: int,
                        body: LeaseTerminateIn, *, ip: str | None = None) -> dict[str, Any]:
        """退协议：先把池内电池还回去，再退押（可退、按原路、v1 人工退汇）。"""
        lease = await self._require_holder_or_operator(session, principal, lease_id)
        if lease["status"] == "terminated":
            return {"lease": lease, "already": True}

        holder_id = lease["holder_account_id"] or lease["holder_org_id"]
        outstanding = await self.repo.leased_batteries_of_holder(
            session, holder_type=lease["holder_type"], holder_id=int(holder_id or 0),
            operator_org_id=lease["operator_org_id"])
        if outstanding:
            raise InvalidState(
                f"名下车辆还装着 {len(outstanding)} 块该运营商池内电池，先退电池或换回自有电池",
                code="battery_not_returned",
                detail={"batteries": [{"vin": b["vin"], "battery_sn": b["battery_sn"]}
                                      for b in outstanding]})

        row = await self.repo.set_lease_status(session, lease_id, "terminated", ended=True)
        assert row is not None
        deposit = await self.repo.deposit_of_lease(session, lease_id, open_only=True)
        deposit_out = None
        if deposit is not None:
            if body.refund_voucher_no:
                deposit_out = await self.repo.set_deposit_status(
                    session, deposit["id"], "refunded", voucher_no=body.refund_voucher_no)
            else:
                # 不退汇就保持 held：报表里"现在押着多少"必须真实反映欠用户的钱
                deposit_out = deposit
        await write_audit(session, actor=principal, action="swap_lease.terminate",
                          resource_type="swap_lease", resource_id=lease_id,
                          before={"status": lease["status"]},
                          after={"status": "terminated", "reason": body.reason,
                                 "deposit_status": (deposit_out or {}).get("status")}, ip=ip)
        await emit(session, aggregate_type="swap_lease", aggregate_id=lease_id,
                   event_type="terminated",
                   payload={"lease_no": row["lease_no"], "reason": body.reason,
                            "deposit_status": (deposit_out or {}).get("status")})
        return {"lease": row, "deposit": deposit_out}

    async def detail(self, session: AsyncSession, principal: Principal,
                     lease_id: int) -> dict[str, Any]:
        lease = await self.repo.get_lease(session, lease_id)
        if lease is None:
            raise NotFound("lease_not_found")
        self._assert_readable(principal, lease)
        deposit = await self.repo.deposit_of_lease(session, lease_id, open_only=False)
        return {"lease": lease, "deposit": deposit}

    async def deposit_position(self, session: AsyncSession, principal: Principal) -> dict:
        """押金三问（收 / 退 / 押）：平台与监管看全部，运营商看自己。"""
        principal.require("deposit", "read")
        orgs: list[int] | None = None
        if not (principal.is_platform or principal.is_regulator):
            orgs = principal.org_ids_for_line("swap_operator")
            if not orgs:
                raise Forbidden("deposit_position_requires_station")
        return {"items": await self.repo.deposit_position(session, orgs)}

    # ══ 内部 ═══════════════════════════════════════════════════════════════
    def _resolve_parties(self, principal: Principal, body: LeaseIn
                         ) -> tuple[int, str, int | None, int | None, str | None]:
        """定协议三方：持有人、对手方运营商、地区。

        运营商替资产车签协议时持有人是 **org**（司机只是操作人，§16.6）；
        个人签时持有人是账号。地区取该 org 的 region（押金按地区分档）。
        """
        swap_orgs = principal.org_ids_for_line("swap_operator")
        if body.operator_org_id is not None:
            operator_org_id = body.operator_org_id
        elif swap_orgs:
            operator_org_id = swap_orgs[0]
        else:
            raise ValidationFailed("必须指定 operator_org_id（你要加入哪个换电运营商）",
                                   code="operator_org_required")

        if principal.is_platform:
            raise ValidationFailed("平台不能替用户签换电协议", code="platform_cannot_sign")

        if body.operator_org_id is not None and swap_orgs and body.operator_org_id in swap_orgs:
            # 运营商给自己的车签：持有人 = 该 org
            return operator_org_id, "org", None, body.operator_org_id, None

        # 落到这里就是"个人签"：持有人 = 当前账号（有没有 org 都不影响——
        # 运营商员工也可能给自己名下的车签协议，只是持有人记人不记 org）。
        # 原来的 `if not principal.orgs: raise` 把条件写反了，正好把个人用户挡在门外。
        return operator_org_id, "account", principal.account_id, None, None

    async def _require_operator_of(self, session: AsyncSession, principal: Principal,
                                   lease_id: int) -> dict[str, Any]:
        principal.require("deposit", "write")
        lease = await self.repo.get_lease(session, lease_id)
        if lease is None:
            raise NotFound("lease_not_found")
        if lease["operator_org_id"] not in principal.org_ids_for_line("swap_operator"):
            raise Forbidden("deposit_write_requires_operator")
        return lease

    async def _require_holder_or_operator(self, session: AsyncSession, principal: Principal,
                                          lease_id: int) -> dict[str, Any]:
        lease = await self.repo.get_lease(session, lease_id)
        if lease is None:
            raise NotFound("lease_not_found")
        self._assert_readable(principal, lease)
        return lease

    def _assert_readable(self, principal: Principal, lease: dict[str, Any]) -> None:
        if principal.is_platform or principal.is_regulator:
            return
        if lease["holder_account_id"] == principal.account_id:
            return
        if lease["holder_org_id"] is not None and lease["holder_org_id"] in principal.org_ids():
            return
        if lease["operator_org_id"] in principal.org_ids_for_line("swap_operator"):
            return
        raise Forbidden("lease_out_of_scope")

    @staticmethod
    def _lease_no() -> str:
        return f"LS{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6]}"

    @staticmethod
    def _deposit_no() -> str:
        return f"DP{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6]}"
