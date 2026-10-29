"""换电执行（M15，`Task-Plan.md` §16.8）——**换电四写同事务的唯一落地点**。

`Task-Server.md` §7 依赖铁律 6：解绑 + 绑定 + 电池状态与位置 + 库存增减 + 换电单 +
流转流水 + outbox，**只能在本文件的一个方法里发生**。router 与 task 都不得直接改
`energy.battery` 或 `energy.station_stock`——否则会出现"车没电池"或"电池凭空增减"
（P21，换电域唯一事故级风险点）。

五道闸门（顺序即优先级，任一不过 → 落一条 `status='rejected'` 的单 + 原因码，再抛错）：
① 型号三层白名单：型号字典 `active` ∩ 站点 `allowed_models` ∩ 车型兼容声明，且旧/新 SN 型号一致；
② 两块电池都在本运营商池内（台账查无 SN = 池外电池，**不接收、不代管**）；
③ 换入电池 `ready` 且 SOC ≥ 价目承诺的 `min_soc_pct`；
④ 换电协议 `active` 且押金 `held`（或免押豁免）；
⑤ 风控：账号 active、无未结赔偿纠纷。

**拒单为什么要单独一个事务写**：拒单本身要能统计（P29——白名单配错必须看得见），
但它不能让请求事务失败回滚（回滚会把拒单记录一起抹掉）。所以拒单走 `core/db.tx()`
的独立事务，然后才抛 `InvalidState`。这与 `fence_repo.validate_wkt` 用独立连接是同一个道理。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.db import tx
from ..core.exceptions import Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..models.core import Vehicle, VehicleType
from ..repositories.base import assert_vehicle_visible
from ..repositories.energy_repo import EnergyRepo
from ..schemas.energy import SwapIn
from .points_service import REFUND_VALIDITY_DAYS, PointsService

RE_SWAP_WINDOW = timedelta(minutes=30)     # §16.5：换出电池 SOC 不达标 → 30 分钟内免费重换
# 积分抵扣上限**不在代码里**：它是 `mkt.points_offset_rule.max_offset_pct`（§16.9 换电默认 20%，
# 按地区/币种分档、版本化）。写死在这里就会出现"代码 20% / 规则表 30%"两个真相。


class SwapService:
    repo = EnergyRepo()
    points = PointsService()

    # ══ 换电执行 ═══════════════════════════════════════════════════════════
    async def execute(self, session: AsyncSession, principal: Principal, body: SwapIn, *,
                      ip: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """一次换电：五道闸门 → 四写同事务 → 审计 + 事件。"""
        # 幂等是硬要求：站点弱网重试不能重复收费、不能重复换绑（§16.8）
        if not idempotency_key:
            raise ValidationFailed("换电必须带 Idempotency-Key 请求头（站点弱网重试防重复收费）",
                                   code="idempotency_key_required")
        existed = await self.repo.find_swap(session, idempotency_key=idempotency_key)
        if existed is not None:
            return {"swap": existed, "replayed": True}

        station = await self.repo.get_station(session, code=body.station_code)
        if station is None:
            raise NotFound("station_not_found")

        vehicle = (await session.execute(
            select(Vehicle).where(Vehicle.vin == body.vin))).scalar_one_or_none()
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        self._assert_can_operate(principal, vehicle, station)

        # ② 两块电池必须在台账里（查不到 = 池外电池，直接拒，不"先放着"）
        out_b = await self.repo.get_battery(session, body.out_battery_sn)
        in_b = await self.repo.get_battery(session, body.in_battery_sn)
        if out_b is None or in_b is None:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="battery_not_registered", ip=ip,
                                      idempotency_key=idempotency_key)

        # 跨运营商禁止（§16.9）：站只收本运营商池内电池
        if out_b["owner_org_id"] != station["org_id"] or in_b["owner_org_id"] != station["org_id"]:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="battery_other_operator", ip=ip,
                                      idempotency_key=idempotency_key)

        # 旧电池必须是这台车当前绑着的那块（扫码扫错 / 现场已被别人换过）
        binding = await self.repo.active_binding(session, vin=body.vin)
        if binding is None or binding["battery_sn"] != body.out_battery_sn:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="binding_mismatch", ip=ip,
                                      idempotency_key=idempotency_key)

        # ① 型号三层白名单
        model = await self.repo.get_model(session, in_b["model_code"])
        if (model is None or model["status"] != "active"
                or in_b["model_code"] not in (station["allowed_models"] or [])
                or out_b["model_code"] != in_b["model_code"]):
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="model_not_allowed", ip=ip,
                                      idempotency_key=idempotency_key)
        vtype = (await session.execute(
            select(VehicleType).where(VehicleType.type_code == vehicle.type_code))
        ).scalar_one_or_none()
        # 兼容是**双向声明**（§16.2）：型号侧必须把该车型列进 compatible_types；
        # 车型侧必须 swap_compatible，且若声明了默认型号则必须与本次型号一致。
        # 任一侧缺配置 → 拒（宁可拒单，也不让一块换不出去的电池进池）。
        model_side = vehicle.type_code in (model["compatible_types"] or [])
        type_side = (vtype is not None and vtype.swap_compatible
                     and (vtype.battery_model_code is None
                          or vtype.battery_model_code == model["model_code"]))
        if not (model_side and type_side):
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="model_mismatch", ip=ip,
                                      idempotency_key=idempotency_key)

        # ④ 协议与押金（协议按"车的归属"解析，发起人只是操作人）
        lease = await self._resolve_lease(session, vehicle, station)
        if lease is None or lease["status"] != "active":
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="lease_inactive", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease)
        if not lease["deposit_waived"]:
            deposit = await self.repo.deposit_of_lease(session, lease["id"], open_only=True)
            if deposit is None or deposit["status"] != "held":
                return await self._reject(session, principal, station=station, vehicle=vehicle,
                                          body=body, reason="deposit_missing", ip=ip,
                                          idempotency_key=idempotency_key, lease=lease)

        # ⑤ 风控（账号冻结）
        if principal.status != "active":
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="risk_blocked", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease)

        # ③ 换入电池必须 ready 且 SOC 达标（承诺值来自生效价目，不是硬编码）
        price = await self.repo.effective_price(session, model_code=model["model_code"],
                                                region_id=station["region_id"],
                                                station_org_id=station["org_id"])
        if price is None:
            raise InvalidState("该型号在该站没有生效价目，无法计价", code="price_not_configured")
        min_soc = int(price["min_soc_pct"])
        if in_b["status"] == "faulty":
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="battery_faulty", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease)
        if in_b["status"] != "ready" or in_b["station_id"] != station["id"]:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="stock_empty", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease)
        if in_b["soc"] is None or int(in_b["soc"]) < min_soc:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="soc_below", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease)

        # 积分抵扣（§16.9）在换电单落库**之后**处理——`fin.subsidy_ledger.order_ref_id`
        # 要挂换电单 id，而抵扣的现金等值又要写回这张单。见下面 `_apply_points_offset`。

        # 30 分钟免费重换（§16.5）：原价 0，且把上次换出去的那块计一次质量投诉
        re_swap = None
        if body.re_swap_of is not None:
            re_swap = await self.repo.find_swap(session, swap_id=body.re_swap_of)
            if (re_swap is None or re_swap["status"] != "completed"
                    or re_swap["station_id"] != station["id"] or re_swap["vin"] != body.vin):
                raise ValidationFailed("免费重换必须指向本站同车的已完成换电单",
                                       code="re_swap_invalid")
            created = re_swap["completed_at"] or re_swap["created_at"]
            now = datetime.now(UTC)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if now - created > RE_SWAP_WINDOW:
                raise InvalidState(f"距上次换电已超过 {RE_SWAP_WINDOW.total_seconds() / 60:.0f} 分钟",
                                   code="re_swap_window_expired")

        # ── 四写同事务（本方法唯一一次落库；异常整体回滚）─────────────────
        # 并发防线：先按 SN 升序锁两块电池，锁不到说明另一笔换电正在动它们
        locked = await self.repo.lock_batteries(session, [body.out_battery_sn, body.in_battery_sn])
        if len(locked) != 2:
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="stock_empty", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease,
                                      note="电池正被另一笔换电占用")

        amount = 0 if re_swap is not None else int(price["price"])
        pricing = {
            "model_code": model["model_code"], "kwh_nominal": str(model["kwh_nominal"]),
            "price_rule_id": price["id"], "price_rule_version": price["version"],
            "unit_price": amount, "min_soc_pct": min_soc,
            "in_soc": in_b["soc"], "free_re_swap": re_swap is not None,
        }
        swap_no = self._swap_no(station["code"])
        order = await self.repo.insert_swap(session, swap={
            "swap_no": swap_no, "station_id": station["id"],
            "operator_org_id": station["org_id"], "lease_id": lease["id"],
            "account_id": self._beneficiary(vehicle, principal), "vin": body.vin,
            "model_code": model["model_code"],
            "out_battery_sn": body.out_battery_sn, "in_battery_sn": body.in_battery_sn,
            "price_rule_id": price["id"], "pricing": pricing,
            "amount": amount, "cash_amount": amount, "points_offset": 0,
            "currency": price["currency"], "collect_mode": "operator_offline",
            "idempotency_key": idempotency_key, "status": "completed",
            "re_swap_of": body.re_swap_of,
        })

        # 积分抵扣（§16.9）：扣积分 + 按 points_kind 分记平台补贴 + 写回本单。
        # 与四写同事务——积分扣了但换电失败，或换电成功但积分没扣，都是账不平。
        # 免费重换 amount=0 → 上限算出来是 0，任何抵扣都会被 `offset_over_cap` 拒。
        if body.points_offset:
            order = await self._apply_points_offset(
                session, order=order, pricing=pricing, points=body.points_offset,
                station=station, currency=price["currency"])

        # 免费重换要把**原单**扣过的积分退回（"免费"不能一边免钱一边留着上次的抵扣）。
        # 退给原单的 account_id，不是操作者的——站点员工替用户重换时退错人就是资损。
        if re_swap is not None and int(re_swap["points_offset"] or 0) > 0:
            await self._refund_points_offset(session, origin=re_swap)

        # 写 1/2/3/4：解绑 + 绑定 + 两块电池状态与位置 + 库存
        try:
            await self.repo.end_binding(session, binding["id"])
            await self.repo.insert_binding(session, vin=body.vin, battery_sn=body.in_battery_sn,
                                           station_id=station["id"], swap_order_id=order["id"],
                                           reason="swap")
            await self.repo.set_battery_state(session, body.out_battery_sn, status="returned",
                                              station_id=station["id"], bump_cycles=True)
            await self.repo.set_battery_state(session, body.in_battery_sn, status="leased",
                                              station_id=None)
            if re_swap is not None and re_swap["in_battery_sn"]:
                await self.repo.set_battery_state(session, re_swap["in_battery_sn"],
                                                  status="returned", station_id=station["id"],
                                                  bump_complaints=True)
            # 换出一块 ready → -1；换回的旧电池是 returned，不计入三个计数
            await self.repo.stock_adjust(session, station_id=station["id"],
                                         model_code=model["model_code"],
                                         ready_delta=-1, charging_delta=0, faulty_delta=0)
        except IntegrityError as e:
            """并发下另一笔换电已经提交（`FOR UPDATE SKIP LOCKED` 只挡住"正在锁"的，
            挡不住"刚提交完"的）：`uq_binding_active` / `uq_binding_battery_active` /
            `station_stock` CHECK 是最后防线（§11.11）。**必须翻译成拒单**——
            站点作业台收到 500 会让人以为没换成而再扫一次，半个绑定留在库里更糟。
            """
            await session.rollback()          # 事务已废：先回滚，再用独立事务落拒单
            why = type(e.orig).__name__ if e.orig is not None else "IntegrityError"
            return await self._reject(session, principal, station=station, vehicle=vehicle,
                                      body=body, reason="stock_empty", ip=ip,
                                      idempotency_key=idempotency_key, lease=lease,
                                      note=f"并发冲突（{why}）：电池或库存已被另一笔换电改动")

        for bat, frm, to, vin_col in (
            (body.out_battery_sn, out_b["status"], "returned", body.vin),
            (body.in_battery_sn, in_b["status"], "leased", body.vin),
        ):
            await self.repo.append_flow(session, battery_sn=bat, from_status=frm, to_status=to,
                                        station_id=station["id"], vin=vin_col,
                                        swap_order_id=order["id"],
                                        actor_account_id=principal.account_id,
                                        actor_role=principal.active_identity,
                                        note=body.note, media_id=None)

        for event, payload in (
            ("verified", {"out": body.out_battery_sn, "in": body.in_battery_sn,
                          "model_code": model["model_code"]}),
            ("swapped", {"binding_id": binding["id"]}),
            ("completed", {"amount": amount, "currency": price["currency"],
                           "free_re_swap": re_swap is not None}),
        ):
            await self.repo.append_swap_event(session, swap_order_id=order["id"], event=event,
                                              payload=payload,
                                              actor_account_id=principal.account_id)

        # 审计与事件都带上"抵扣前金额 / 现金 / 抵扣点数"三件套：
        # 运营商应收按 amount 算、用户实付是 cash_amount，事后对账只看这一个字段就够（§10-14）
        await write_audit(session, actor=principal, action="swap.execute",
                          resource_type="swap_order", resource_id=order["id"],
                          after={"swap_no": swap_no, "vin": body.vin, "station_id": station["id"],
                                 "out_battery_sn": body.out_battery_sn,
                                 "in_battery_sn": body.in_battery_sn,
                                 "amount": amount, "cash_amount": order["cash_amount"],
                                 "points_offset": order["points_offset"],
                                 "currency": price["currency"]}, ip=ip)
        await emit(session, aggregate_type="swap_order", aggregate_id=order["id"],
                   event_type="completed",
                   payload={"swap_no": swap_no, "vin": body.vin,
                            "station_id": station["id"], "operator_org_id": station["org_id"],
                            "model_code": model["model_code"], "amount": amount,
                            "cash_amount": order["cash_amount"],
                            "points_offset": order["points_offset"],
                            "currency": price["currency"]})
        return {"swap": order, "replayed": False}

    # ══ 积分抵扣（§16.9 / §7.3 / §10-14）════════════════════════════════════
    async def _apply_points_offset(self, session: AsyncSession, *, order: dict, pricing: dict,
                                   points: int, station: dict, currency: str) -> dict:
        """扣积分 + 记平台补贴 + 把抵扣写回换电单（同事务）。

        **扣的是 `swap_order.account_id` 的积分**——该 id 由协议持有人推导
        （`_resolve_parties`），不是按按钮的站点员工。按操作者扣积分会出现
        "员工的积分抵给了用户的单"这种资损，所以这里不接 principal。

        **`amount` 不动**：运营商按抵扣前金额收款（§10-14），站点作业台回填校验用
        `cash_amount`；差额已经由 `points_service.spend()` 按 `points_kind` 分记进
        `fin.subsidy_ledger`，所以这里只改 `cash_amount` / `points_offset` / `pricing` 快照。
        """
        plan = await self.points.spend(
            session, account_id=order["account_id"], points=points, biz_line="swap",
            amount=int(order["amount"]), currency=currency, region_id=station["region_id"],
            order_ref_type="swap_order", order_ref_id=order["id"],
            source_type="swap_offset", source_ref=order["swap_no"],
            beneficiary_type="org", beneficiary_id=order["operator_org_id"],
            memo=f"换电积分抵扣 {order['swap_no']}")
        snap = dict(pricing)
        snap.update({"points_offset": plan["points"], "offset_cash_value": plan["cash_value"],
                     "offset_rule": plan["rule"], "offset_split": plan["split"]})
        updated = await self.repo.apply_offset(
            session, order["id"], points_offset=plan["points"],
            cash_amount=int(order["amount"]) - int(plan["cash_value"]), pricing=snap)
        if updated is None:
            raise InvalidState("换电单抵扣写回失败", code="offset_apply_failed")
        return updated

    async def _refund_points_offset(self, session: AsyncSession, *, origin: dict) -> None:
        """免费重换 → 退回原单扣过的积分（冲正，append-only）。

        `source_ref` 用 `原单号:reswap`：与扣减的 `swap_offset/原单号` 不同键，
        但同一原单重复重换只会退一次（撞 `uq_ledger_source` → `has_ledger` 预查直接返回）。
        退回批次给 90 天有效期（§7.1 不得无限期），不复活原批次（可能已过期）。
        """
        split = {str(d["points_kind"]): int(d["points"])
                 for d in (origin.get("pricing") or {}).get("offset_split", [])}
        await self.points.refund(
            session, account_id=origin["account_id"], points=int(origin["points_offset"]),
            source_type="swap_offset_refund", source_ref=f"{origin['swap_no']}:reswap",
            expires_at=datetime.now(UTC) + timedelta(days=REFUND_VALIDITY_DAYS),
            kinds=split or None, memo=f"免费重换退回（原单 {origin['swap_no']}）")

    # ══ 查询 ═══════════════════════════════════════════════════════════════
    async def detail(self, session: AsyncSession, principal: Principal, swap_id: int) -> dict:
        row = await self.repo.find_swap(session, swap_id=swap_id)
        if row is None:
            raise NotFound("swap_not_found")
        self._assert_swap_readable(principal, row)
        return {"swap": row, "events": await self.repo.swap_events(session, swap_id)}

    async def page(self, session: AsyncSession, principal: Principal, *, status: str | None = None,
                   station_id: int | None = None, limit: int = 50,
                   offset: int = 0) -> dict:
        from ..repositories.base import swap_scope

        sub = swap_scope(principal).subquery()
        items, total = await self.repo.list_swaps(session, scope_subquery=select(sub.c.id),
                                                  status=status, station_id=station_id,
                                                  limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def rejections(self, session: AsyncSession, principal: Principal, *,
                         station_id: int | None = None, days: int = 7) -> dict:
        if not (principal.is_platform or principal.is_regulator):
            orgs = principal.org_ids_for_line("swap_operator")
            if not orgs:
                raise Forbidden("swap_reject_stats_requires_station")
            if station_id is None:
                raise ValidationFailed("station_id 必填", code="station_id_required")
            st = await self.repo.get_station(session, station_id=station_id)
            if st is None or st["org_id"] not in orgs:
                raise Forbidden("station_out_of_scope")
        since = datetime.now(UTC) - timedelta(days=max(1, min(days, 90)))
        return {"since": since.isoformat(),
                "items": await self.repo.rejection_stats(session, station_id=station_id,
                                                         since=since)}

    # ══ 内部 ═══════════════════════════════════════════════════════════════
    def _assert_can_operate(self, principal: Principal, vehicle: Vehicle,
                            station: dict[str, Any]) -> None:
        """谁能发起这笔换电：本站换电运营商（作业台）或对该车有权限的人（车主/司机）。"""
        if station["org_id"] in principal.org_ids_for_line("swap_operator"):
            return
        assert_vehicle_visible(principal, vehicle)

    @staticmethod
    def _beneficiary(vehicle: Vehicle, principal: Principal) -> int:
        """`swap_order.account_id` 记**车主（计费与受益人）**，不是发起人。

        站点作业台是站内人员代刷卡：发起人是员工/平台账号，但
        ① 车主在自己的用户端必须看得见这笔换电（`swap_scope` 只按 `account_id` 收敛）；
        ② 返积分与免费重换退回都按 `account_id` 记账（`points_service.award/refund`），
           记成员工就是给员工发积分。
        发起人另有两处：`energy.swap_event.actor_account_id` 与 `audit` 的 actor。
        资产车（`owner_type='org'`）没有对应 account，退化为发起人。
        """
        return vehicle.owner_account_id or principal.account_id

    async def _resolve_lease(self, session: AsyncSession, vehicle: Vehicle,
                             station: dict[str, Any]) -> dict[str, Any] | None:
        """协议按**车的归属**解析（车会过户、司机会换车，§16.6）。"""
        if vehicle.owner_type == "org" and vehicle.owner_org_id is not None:
            return await self.repo.find_lease(session, org_id=vehicle.owner_org_id,
                                              operator_org_id=station["org_id"])
        if vehicle.owner_account_id is not None:
            return await self.repo.find_lease(session, account_id=vehicle.owner_account_id,
                                              operator_org_id=station["org_id"])
        return None

    def _assert_swap_readable(self, principal: Principal, row: dict[str, Any]) -> None:
        if principal.is_platform or principal.is_regulator:
            return
        if row["account_id"] == principal.account_id:
            return
        if row["operator_org_id"] in principal.org_ids_for_line("swap_operator"):
            return
        raise Forbidden("swap_out_of_scope")

    async def _reject(self, session: AsyncSession, principal: Principal, *, station: dict,
                      vehicle: Vehicle, body: SwapIn, reason: str, ip: str | None,
                      idempotency_key: str | None, lease: dict | None = None,
                      note: str | None = None) -> dict:
        """拒单：独立事务落库（可统计），然后抛 InvalidState(code=原因码)。

        为什么不用请求 session：抛错会让请求事务回滚，拒单记录一起消失，
        P29（白名单配错 / 池外电池混入）就没有任何可观测面。
        """
        async with tx() as s2:
            order = await self.repo.insert_swap(s2, swap={
                "swap_no": self._swap_no(station["code"]),
                "station_id": station["id"], "operator_org_id": station["org_id"],
                "lease_id": lease["id"] if lease else None,
                "account_id": self._beneficiary(vehicle, principal), "vin": body.vin,
                "out_battery_sn": body.out_battery_sn, "in_battery_sn": body.in_battery_sn,
                "amount": 0, "cash_amount": 0, "points_offset": 0,
                "status": "rejected", "reject_reason": reason,
                "idempotency_key": idempotency_key,
                "pricing": {"note": note} if note else {},
            })
            await self.repo.append_swap_event(s2, swap_order_id=order["id"], event="rejected",
                                              payload={"reason": reason, "note": note},
                                              actor_account_id=principal.account_id)
            await write_audit(s2, actor=principal, action="swap.rejected",
                              resource_type="swap_order", resource_id=order["id"],
                              after={"reject_reason": reason, "station_id": station["id"],
                                     "vin": body.vin, "out_battery_sn": body.out_battery_sn,
                                     "in_battery_sn": body.in_battery_sn, "note": note}, ip=ip)
        raise InvalidState(f"换电被拒绝：{reason}", code=reason,
                           detail={"swap_no": order["swap_no"], "reject_reason": reason})

    @staticmethod
    def _swap_no(station_code: str) -> str:
        from uuid import uuid4

        return f"SW{station_code[:8].upper()}{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6]}"
