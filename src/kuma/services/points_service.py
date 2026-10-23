"""积分账本的业务层（`Task-Plan.md` §7.1–§7.6 / §16.9）。

覆盖四件事：**消耗**（抵扣，与业务同事务）、**退回/冲正**（append-only）、
**发放**（返积分与买包入账，`grant()`）、**规则配置**（抵扣规则 + 返积分规则，双人授权），
外加两条日端口径：**过期清零**与**积分负债快照**（§7.6 / §7.5）。

**为什么抵扣必须和业务写在同一个事务里**：换电那边"四写同事务"再加一笔——
积分扣了但换电失败 = 用户白扣积分；换电成功但积分没扣 = 白送一次抵扣，
而且 `fin.subsidy_ledger`（平台该补贴给运营商的钱）会少记，月底对不上账。
所以 `spend()` 只接受**调用方已开启的事务 session**，自己绝不开新事务
（与 `swap_service` 的拒单相反：拒单要独立事务落库，因为请求事务注定回滚）。

三条口径写在这里而不是散在各调用点：
- **上限按规则算**（§7.3 货款 30% / 车费 20%，§16.9 换电 20%），
  `purchased` 与 `earned` **合并计算**（§7.4 防两类额度叠加）；
- **抵扣部分不产生积分**（§7.4），所以调用方返积分时必须用**实付现金**做基数；
- **收款方按抵扣前金额收款**（§10-14），差额进 `fin.subsidy_ledger` 且
  **按 `points_kind` 分记**（§7.5：`purchased` 冲合同负债、`earned` 是营销费用）。

金额换算只有一处：`cash = points × unit_scale ÷ points_per_unit`（向下取整）。
`points_per_unit` = 1 主币单位值多少积分（本轮 100），`unit_scale` = 1 主币单位含多少
最小货币单位（THB/MYR=100）。取整余数**留在现金侧**——宁可让用户少抵一分，
不能让平台多贴一分，也不能出现"抵扣额加起来比订单还大"。
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import InvalidState, ValidationFailed
from ..core.scope import Principal
from ..repositories.points_repo import PointsRepo
from .dual_control import require_co_approver

KINDS = ("purchased", "earned")
#: 退回积分时新批次的默认有效期（§7.1"不得无限期"；退回不该给用户更长的期限）
REFUND_VALIDITY_DAYS = 90
#: 返积分流水的来源值（与 `points_rule.source_type='swap'` 配对；
#: 上限统计与补发 job 都按这个值找流水，改名等于把历史流水变成孤儿）
AWARD_SOURCE_SWAP = "swap_award"
EXPIRE_SOURCE = "points_expire"


def add_months(moment: datetime, months: int) -> datetime:
    """按**日历月**加期限（§7.1"赠送积分 12 个月"、§7.2"购买积分按包，默认 24 个月"）。

    不用 `timedelta(days=months*30)`：那样 12 个月差 5 天，24 个月差 10 天，
    而到期时间是给用户看、也要进负债表的硬日期。月末溢出按当月最后一天截断
    （1/31 + 1 月 = 2/28，不是 3/3）。
    """
    year, month0 = divmod(moment.month - 1 + months, 12)
    month = month0 + 1
    day = min(moment.day, calendar.monthrange(moment.year + year, month)[1])
    return moment.replace(year=moment.year + year, month=month, day=day)


class PointsService:
    repo = PointsRepo()

    # ══ 查询 ═══════════════════════════════════════════════════════════════
    async def balance(self, session: AsyncSession, principal: Principal) -> dict[str, Any]:
        """余额分两栏（§9.1 用户端"分'购买'与'赠送'两栏"）+ 30 天内到期额。"""
        principal.require("points", "read")
        acc = await self.repo.get_account(session, principal.account_id)
        return {
            "earned": int(acc["earned_balance"]) if acc else 0,
            "purchased": int(acc["purchased_balance"]) if acc else 0,
            "total": (int(acc["earned_balance"]) + int(acc["purchased_balance"])) if acc else 0,
            "frozen": bool(acc["frozen"]) if acc else False,
            "expiring_30d": await self.repo.expiring_soon(session, principal.account_id),
        }

    async def ledger(self, session: AsyncSession, principal: Principal, *, limit: int = 50,
                     offset: int = 0) -> list[dict[str, Any]]:
        principal.require("points", "read")
        return await self.repo.ledger_of(session, principal.account_id, limit=limit, offset=offset)

    async def offset_capacity(self, session: AsyncSession, principal: Principal, *,
                              biz_line: str, amount: int, currency: str,
                              region_id: str | None) -> dict[str, Any]:
        """本单最多能抵多少（前端显示"最多可抵 X"，不扣任何东西）。"""
        principal.require("points", "read")
        rule = await self.repo.effective_offset_rule(session, biz_line=biz_line,
                                                     currency=currency, region_id=region_id)
        if rule is None:
            return {"available": False, "reason": "offset_rule_missing", "max_points": 0,
                    "max_cash": 0}
        acc = await self.repo.get_account(session, principal.account_id)
        owned = ((int(acc["earned_balance"]) + int(acc["purchased_balance"])) if acc else 0)
        max_cash = amount * int(rule["max_offset_pct"]) // 100
        by_cap = max_cash * int(rule["points_per_unit"]) // int(rule["unit_scale"])
        return {
            "available": True,
            "rule_id": rule["id"], "rule_version": rule["version"],
            "points_per_unit": int(rule["points_per_unit"]),
            "unit_scale": int(rule["unit_scale"]),
            "max_offset_pct": int(rule["max_offset_pct"]),
            "max_cash": max_cash,
            "max_points": min(by_cap, owned),
            "owned": owned,
        }

    # ══ 消耗（与业务同事务）════════════════════════════════════════════════
    async def spend(self, session: AsyncSession, *, account_id: int, points: int,
                    biz_line: str, amount: int, currency: str, region_id: str | None,
                    order_ref_type: str, order_ref_id: int, source_type: str, source_ref: str,
                    beneficiary_type: str, beneficiary_id: int,
                    memo: str | None = None) -> dict[str, Any]:
        """扣**指定账户**的积分 + 更新快照 + 按 `points_kind` 分记平台补贴。

        `points == 0` 直接返回空明细（调用方不必自己加分支）。

        **不收 principal、不查 `points:write`**（与 `refund` 同一立场，理由在这里更要命）：
        积分属于**单据的持有人**，不属于按下按钮的人。换电是站点作业台提交的
        （`swap:write` 只有 operator_staff 有），若按操作者判权限，
        要么 operator 被 `points:write` 挡死（实测就是这个错），
        要么更糟——按操作者取账户，把**员工的**积分抵给了用户的单，直接资损。
        所以调用方必须显式传 `account_id`（换电取 `swap_order.account_id`，
        车费取订单 rider），权限由业务动作本身判定（`swap:write` / `ride:write`）。
        """
        if points <= 0:
            return {"points": 0, "cash_value": 0, "split": [], "rule": None}

        # ① 先锁账户（锁顺序：账户 → 批次，全项目统一，见 points_repo 文档）
        acc = await self.repo.lock_account(session, account_id)
        if acc["frozen"]:
            raise InvalidState("积分已被冻结（对账或风控），暂不可抵扣", code="points_frozen")

        # ② 规则与上限（§7.3 / §16.9）
        rule = await self.repo.effective_offset_rule(session, biz_line=biz_line,
                                                     currency=currency, region_id=region_id)
        if rule is None:
            raise ValidationFailed(f"{biz_line} 未配置生效的积分抵扣规则",
                                   code="offset_rule_missing")
        ppu, scale = int(rule["points_per_unit"]), int(rule["unit_scale"])
        cash_value = points * scale // ppu
        cap_cash = amount * int(rule["max_offset_pct"]) // 100
        if cash_value > cap_cash:
            raise ValidationFailed(
                f"抵扣超限：本单最多抵 {cap_cash}（{rule['max_offset_pct']}%），"
                f"申请抵 {cash_value}", code="offset_over_cap")
        if rule["min_amount"] is not None and amount < int(rule["min_amount"]):
            raise ValidationFailed("订单金额低于抵扣门槛", code="offset_min_amount")

        # ③ 锁批次并按 expires_at 升序扣（§7.6 先扣快过期的）
        batches = await self.repo.lock_batches(session, account_id)
        owned = sum(int(b["remaining"]) for b in batches)
        if points > owned:
            raise InvalidState(f"积分不足：可用 {owned}，申请 {points}", code="points_insufficient")

        need = points
        updates: list[tuple[int, int]] = []
        split: dict[str, int] = {}
        for b in batches:
            if need <= 0:
                break
            take = min(int(b["remaining"]), need)
            updates.append((int(b["id"]), take))
            split[str(b["points_kind"])] = split.get(str(b["points_kind"]), 0) + take
            need -= take
        await self.repo.spend_batches(session, updates=updates)

        # ④ 快照 + 流水（每类一条；`uq_ledger_source` 保证同一单不重复扣）
        earned, purchased = int(acc["earned_balance"]), int(acc["purchased_balance"])
        for kind, amt in split.items():
            if kind == "earned":
                earned -= amt
            else:
                purchased -= amt
            await self.repo.insert_ledger(session, account_id=account_id,
                                          points_kind=kind, delta=-amt,
                                          balance_after=earned if kind == "earned" else purchased,
                                          source_type=source_type, source_ref=source_ref,
                                          memo=memo)
        await self.repo.set_balances(session, account_id=account_id,
                                     earned=earned, purchased=purchased)

        # ⑤ 平台补贴按类分记（§10-14 + §7.5）。取整余数补给扣得最多的那类，
        #    保证 Σsubsidy == swap_order.points_offset 的现金等值，账能对平。
        detail: list[dict[str, Any]] = []
        booked = 0
        for kind, amt in sorted(split.items(), key=lambda kv: -kv[1]):
            cash = amt * scale // ppu
            detail.append({"points_kind": kind, "points": amt, "cash_value": cash})
            booked += cash
        if detail and booked != cash_value:            # 余数（0..ppu-1 之间的最小单位）
            detail[0]["cash_value"] += cash_value - booked
        for d in detail:
            await self.repo.insert_subsidy(session, order_ref_type=order_ref_type,
                                           order_ref_id=order_ref_id, reason="points_offset",
                                           points_kind=d["points_kind"],
                                           amount=d["cash_value"], currency=currency,
                                           beneficiary_type=beneficiary_type,
                                           beneficiary_id=beneficiary_id)
        return {"points": points, "cash_value": cash_value, "split": detail,
                "rule": {"id": rule["id"], "version": rule["version"],
                         "points_per_unit": ppu, "unit_scale": scale,
                         "max_offset_pct": int(rule["max_offset_pct"])}}

    # ══ 退回 / 冲正（append-only：只能插正数流水）════════════════════════════
    async def refund(self, session: AsyncSession, *, account_id: int, points: int,
                     source_type: str, source_ref: str, expires_at: datetime,
                     kinds: dict[str, int] | None = None, memo: str | None = None,
                     ) -> dict[str, Any]:
        """把已扣的积分退回**指定账户**。

        **不收 principal、不查 `points:write`**：退回是业务流程的一环（免费重换、退款、
        争议处理），权限在调用方那侧已经判过。更要命的是——站点员工替用户重换时，
        按 principal 退钱会把积分退到**员工**账上。所以这里只认 `account_id`，
        由调用方从原始单据取（`swap_order.account_id`）。

        **不能改历史流水**（`points_ledger` 对 `kuma_app` 无 UPDATE 权限），
        只能插一条正数流水 + 新开批次（原批次可能已过期，复活它等于绕过过期规则）。
        `source_ref` 必须与原扣减不同，否则撞 `uq_ledger_source`——这正是幂等：
        同一笔退回请求重复调用只会成功一次。
        """
        if points <= 0:
            return {"points": 0}
        if await self.repo.has_ledger(session, source_type=source_type, source_ref=source_ref):
            return {"points": 0, "replayed": True}

        acc = await self.repo.lock_account(session, account_id)
        earned, purchased = int(acc["earned_balance"]), int(acc["purchased_balance"])
        # 默认按"先退 purchased（现金买来的，负债要还原）"退回
        plan = kinds or ({"purchased": points} if purchased >= points
                         else {"purchased": purchased, "earned": points - purchased})
        plan = {k: v for k, v in plan.items() if v > 0}
        if sum(plan.values()) != points:
            raise ValidationFailed("退回明细与退回总额不符", code="refund_split_mismatch")

        for kind, amt in plan.items():
            await self.repo.restore_batches(session, account_id=account_id,
                                            points_kind=kind, amount=amt,
                                            source_type=source_type, source_ref=source_ref,
                                            expires_at=expires_at)
            if kind == "earned":
                earned += amt
            else:
                purchased += amt
            await self.repo.insert_ledger(session, account_id=account_id,
                                          points_kind=kind, delta=amt,
                                          balance_after=earned if kind == "earned" else purchased,
                                          source_type=source_type, source_ref=source_ref,
                                          memo=memo)
        await self.repo.set_balances(session, account_id=account_id,
                                     earned=earned, purchased=purchased)
        return {"points": points, "split": plan, "replayed": False}

    # ══ 发放（买车 / 商城 / 打车 / 换电返积分；worker 侧调用）═════════════════
    async def grant(self, session: AsyncSession, *, account_id: int, points: int,
                    points_kind: str, source_type: str, source_ref: str,
                    expires_at: datetime, memo: str | None = None) -> dict[str, Any]:
        """发放积分：开批次 + 记流水 + 更新快照，幂等靠 `uq_ledger_source`。

        不要求 `points:write`——发放由系统事件触发（§7.2"交易 `completed`/`settled` 才发"），
        人工发放走管理端双人授权（§9.5，尚未实现）。
        """
        if points <= 0:
            raise ValidationFailed("发放点数必须为正", code="grant_points_positive")
        if points_kind not in KINDS:
            raise ValidationFailed(f"points_kind 只能是 {KINDS}", code="grant_kind_invalid")
        if await self.repo.has_ledger(session, source_type=source_type, source_ref=source_ref,
                                      points_kind=points_kind):
            return {"account_id": account_id, "points": 0, "replayed": True}

        acc = await self.repo.lock_account(session, account_id)
        await self.repo.restore_batches(session, account_id=account_id, points_kind=points_kind,
                                        amount=points, source_type=source_type,
                                        source_ref=source_ref, expires_at=expires_at)
        earned, purchased = int(acc["earned_balance"]), int(acc["purchased_balance"])
        if points_kind == "earned":
            earned += points
        else:
            purchased += points
        await self.repo.insert_ledger(session, account_id=account_id, points_kind=points_kind,
                                      delta=points,
                                      balance_after=earned if points_kind == "earned" else purchased,
                                      source_type=source_type, source_ref=source_ref, memo=memo)
        await self.repo.set_balances(session, account_id=account_id,
                                     earned=earned, purchased=purchased)
        return {"account_id": account_id, "points": points, "replayed": False}

    # ══ 抵扣规则配置（平台，双人授权 §9.5）══════════════════════════════════
    async def propose_offset_rule(self, session: AsyncSession, principal: Principal, *,
                                  rule_code: str, biz_line: str, currency: str,
                                  points_per_unit: int, max_offset_pct: int,
                                  version: int, effective_from: date,
                                  region_id: str | None = None, unit_scale: int = 100,
                                  min_amount: int | None = None, effective_to: date | None = None,
                                  co_approver_account_id: int | None = None,
                                  ip: str | None = None) -> dict[str, Any]:
        if not principal.is_platform:
            raise InvalidState("抵扣规则只能由平台配置", code="rule_requires_platform")
        principal.require("points_rule", "write")
        if principal.needs_dual_control("points_rule", "write"):
            await require_co_approver(session, principal, co_approver_account_id)

        rule = await self.repo.insert_offset_rule(
            session, rule_code=rule_code, biz_line=biz_line, region_id=region_id,
            currency=currency, points_per_unit=points_per_unit, unit_scale=unit_scale,
            max_offset_pct=max_offset_pct, min_amount=min_amount, version=version,
            effective_from=effective_from, effective_to=effective_to,
            status="pending_review", proposed_by=principal.account_id)
        await write_audit(session, actor=principal, action="points.offset_rule.propose",
                          resource_type="points_offset_rule", resource_id=rule["id"],
                          after={k: rule[k] for k in ("rule_code", "biz_line", "region_id",
                                                      "currency", "points_per_unit",
                                                      "unit_scale", "max_offset_pct", "version")},
                          ip=ip)
        return rule

    async def approve_offset_rule(self, session: AsyncSession, principal: Principal, *,
                                  rule_id: int, approve: bool,
                                  co_approver_account_id: int | None = None,
                                  ip: str | None = None) -> dict[str, Any]:
        if not principal.is_platform:
            raise InvalidState("抵扣规则只能由平台审核", code="rule_requires_platform")
        principal.require("points_rule", "write")
        if principal.needs_dual_control("points_rule", "write"):
            # 统一走 require_co_approver：只判"非本人"是假的二人——
            # 传一个不存在的账号 id 也能过，审计里的"联审人"就成了空签字
            await require_co_approver(session, principal, co_approver_account_id)

        rule = await self.repo.set_offset_rule_status(
            session, rule_id=rule_id, status="active" if approve else "draft",
            approver=principal.account_id)
        if rule is None:
            raise InvalidState("规则不存在", code="rule_not_found")
        await write_audit(session, actor=principal,
                          action="points.offset_rule.approve" if approve
                          else "points.offset_rule.reject",
                          resource_type="points_offset_rule", resource_id=rule_id,
                          after={"status": rule["status"]}, ip=ip)
        return rule

    # ══ 返积分（§7.2：换电按**实付现金** × 比例；抵扣部分不产生积分）════════════
    async def award_for_swap(self, session: AsyncSession, *, swap_no: str) -> dict[str, Any]:
        """一笔换电完成后返积分（worker 消费 `swap_order.completed` 调这里）。

        **业务规则放在 services，不放 tasks**（`Task-Server.md` §7 依赖铁律：
        tasks 全部走 services，不写业务规则副本）——补发 job 与消费方必须走同一条逻辑，
        否则两条路径迟早算出两个数。

        口径逐条对应 §7.2 换电行：
        - 基数是 `cash_amount`（**实付现金**），不是 `amount`——
          "抵扣部分不产生积分"，用 amount 就是积分生积分（套利的根）；
        - 比例来自 `mkt.points_rule`（`source_type='swap'`，按 region + currency 匹配、版本化），
          **代码里没有默认比例**：没有生效规则就是不发，并返回可观测的原因；
        - `caps.per_order_max` / `per_day_max` 超限**整笔不发**（不是"发一部分"）：
          上限是防刷，静默截断等于给刷单设了个收益率上限而不是拦住它；
        - 幂等靠 `uq_ledger_source(source_type, source_ref, points_kind)`，
          `source_ref = swap_no` → 事件重投、补发 job 重复扫都只发一次。
        """
        order = await self.repo.swap_for_award(session, swap_no)
        if order is None:
            return {"skipped": "swap_not_found", "swap_no": swap_no}
        if order["status"] != "completed":
            # 事件可能乱序或来自被拒单：状态以库里的单据为准，不信 payload
            return {"skipped": f"status_{order['status']}", "swap_no": swap_no}
        cash = int(order["cash_amount"])
        if cash <= 0:
            # 免费重换 / 全额抵扣 → 没有实付现金，不产生积分
            return {"skipped": "no_cash_basis", "swap_no": swap_no}

        rule = await self.repo.effective_award_rule(
            session, source_type="swap", currency=str(order["currency"]),
            region_id=order.get("region_id"))
        if rule is None:
            return {"skipped": "award_rule_missing", "swap_no": swap_no,
                    "currency": order["currency"], "region_id": order.get("region_id")}

        points = self._award_points(rule, cash)
        if points <= 0:
            return {"skipped": "rounds_to_zero", "swap_no": swap_no, "cash_amount": cash}

        caps = rule.get("caps") or {}
        per_order = caps.get("per_order_max")
        if per_order is not None and points > int(per_order):
            return {"skipped": "over_order_cap", "swap_no": swap_no, "points": points,
                    "per_order_max": int(per_order)}
        per_day = caps.get("per_day_max")
        account_id = int(order["account_id"])
        if per_day is not None:
            midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            got = await self.repo.awarded_since(session, account_id=account_id,
                                                source_type=AWARD_SOURCE_SWAP, since=midnight)
            if got + points > int(per_day):
                return {"skipped": "over_daily_cap", "swap_no": swap_no, "points": points,
                        "awarded_today": got, "per_day_max": int(per_day)}

        expires_at = add_months(datetime.now(UTC), int(rule["validity_months"]))
        out = await self.grant(session, account_id=account_id, points=points,
                               points_kind="earned", source_type=AWARD_SOURCE_SWAP,
                               source_ref=swap_no, expires_at=expires_at,
                               memo=f"换电返积分（规则 {rule['rule_code']} v{rule['version']}）")
        return {"granted": points, "replayed": out["replayed"], "swap_no": swap_no,
                "account_id": account_id, "rule": rule["rule_code"],
                "rule_version": rule["version"], "expires_at": expires_at.isoformat()}

    @staticmethod
    def _award_points(rule: dict[str, Any], cash_amount: int) -> int:
        """按规则算返积分点数。

        `ratio` 是"**每 1 最小货币单位**返多少积分"（金额列是最小单位，§3.2），
        所以 THB 下"实付 1 泰铢返 1 积分" = `ratio = 0.01`。
        用 Decimal 算并**向下取整**：float 乘出来的 34.999999 会变成 34 积分，
        而多给一分积分就是白送钱（宁少发不多发，与抵扣侧同一立场）。
        """
        if rule.get("fixed_points") is not None:
            return int(rule["fixed_points"])
        if rule.get("ratio") is None:
            return 0
        return int(Decimal(str(cash_amount)) * Decimal(str(rule["ratio"])))

    async def propose_award_rule(self, session: AsyncSession, principal: Principal, *,
                                 rule_code: str, source_type: str, region_id: str | None,
                                 currency: str | None, version: int, effective_from: date,
                                 ratio: float | None = None, fixed_points: int | None = None,
                                 type_code: str | None = None, caps: dict[str, Any]
                                 | None = None, effective_to: date | None = None,
                                 validity_months: int = 12,
                                 co_approver_account_id: int | None = None,
                                 ip: str | None = None) -> dict[str, Any]:
        """申报返积分规则（§9.5 积分发放规则：平台 RW、**双人授权**）。

        `ratio` 与 `fixed_points` 二选一：前者按实付现金比例（换电/商城/打车），
        后者按单固定点数（买车按车型）。两个都给会无法判定优先级 → 直接拒。
        """
        if not principal.is_platform:
            raise InvalidState("返积分规则只能由平台配置", code="rule_requires_platform")
        principal.require("points_rule", "write")
        if principal.needs_dual_control("points_rule", "write"):
            await require_co_approver(session, principal, co_approver_account_id)
        if (ratio is None) == (fixed_points is None):
            raise ValidationFailed("ratio 与 fixed_points 必须且只能给一个",
                                   code="award_rule_basis_ambiguous")
        if ratio is not None and not 0 < float(ratio) < 100:
            raise ValidationFailed("ratio 必须在 (0, 100) 之间（每最小货币单位返多少积分）",
                                   code="award_ratio_out_of_range")
        if validity_months <= 0:
            raise ValidationFailed("validity_months 必须为正（§7.1 赠送积分 12 个月）",
                                   code="award_validity_invalid")
        if effective_to is not None and effective_to <= effective_from:
            raise ValidationFailed("effective_to 必须晚于 effective_from", code="date_range")

        rule = await self.repo.insert_award_rule(
            session, rule_code=rule_code, source_type=source_type, region_id=region_id,
            currency=currency, type_code=type_code, ratio=ratio, fixed_points=fixed_points,
            caps=caps or {}, version=version, effective_from=effective_from,
            effective_to=effective_to, validity_months=validity_months, status="pending_review",
            proposed_by=principal.account_id)
        await write_audit(session, actor=principal, action="points.award_rule.propose",
                          resource_type="points_rule", resource_id=rule["id"],
                          after={"rule_code": rule_code, "source_type": source_type,
                                 "ratio": ratio, "fixed_points": fixed_points,
                                 "version": version, "validity_months": validity_months,
                                 "co_approver_account_id": co_approver_account_id}, ip=ip)
        return rule

    async def approve_award_rule(self, session: AsyncSession, principal: Principal, *,
                                 rule_id: int, approve: bool,
                                 co_approver_account_id: int | None = None,
                                 ip: str | None = None) -> dict[str, Any]:
        """审核返积分规则：通过后立即对所有后续换电生效，**不追溯已发的积分**（§7.2）。"""
        if not principal.is_platform:
            raise InvalidState("返积分规则只能由平台审核", code="rule_requires_platform")
        principal.require("points_rule", "write")
        if principal.needs_dual_control("points_rule", "write"):
            await require_co_approver(session, principal, co_approver_account_id)

        rule = await self.repo.set_award_rule_status(
            session, rule_id=rule_id, status="active" if approve else "draft",
            approver=principal.account_id)
        if rule is None:
            raise InvalidState("规则不存在", code="rule_not_found")
        await write_audit(session, actor=principal,
                          action="points.award_rule.approve" if approve
                          else "points.award_rule.reject",
                          resource_type="points_rule", resource_id=rule_id,
                          after={"status": rule["status"]}, ip=ip)
        return rule

    # ══ 过期清零（§7.6：worker 每日扫 `expires_at` 生成过期流水）═══════════════
    async def expire_batches(self, session: AsyncSession, *, limit: int = 500) -> dict[str, Any]:
        """把过期批次清零并记负数流水——**不自动改账**之外的唯一自动动作就是这条，
        因为它是规则本身（§7.1 期限），不是对账差异。

        锁顺序仍是 **账户 → 批次**（`due_batches` 已按 account_id 升序返回，
        全局顺序一致才不会和消耗路径死锁）。
        """
        due = await self.repo.due_batches(session, limit=limit)
        acc_cache: dict[int, dict[str, Any]] = {}
        done, total_points = 0, 0
        for b in due:
            acc_id = int(b["account_id"])
            acc = acc_cache.get(acc_id)
            if acc is None:
                acc = await self.repo.lock_account(session, acc_id)
                acc_cache[acc_id] = acc
            zeroed = await self.repo.zero_batch(session, int(b["id"]))
            if zeroed is None:
                continue                      # 并发下已被处理（或刚被续期），跳过
            amount, kind = int(zeroed["remaining"]), str(zeroed["points_kind"])
            if kind == "earned":
                acc["earned_balance"] = int(acc["earned_balance"]) - amount
            else:
                acc["purchased_balance"] = int(acc["purchased_balance"]) - amount
            await self.repo.insert_ledger(
                session, account_id=acc_id, points_kind=kind, delta=-amount,
                balance_after=int(acc["earned_balance"] if kind == "earned"
                                  else acc["purchased_balance"]),
                source_type=EXPIRE_SOURCE, source_ref=f"batch:{b['id']}",
                memo="积分到期（§7.1）")
            await self.repo.set_balances(session, account_id=acc_id,
                                         earned=int(acc["earned_balance"]),
                                         purchased=int(acc["purchased_balance"]))
            done += 1
            total_points += amount
        return {"batches": done, "points": total_points, "scanned": len(due)}
