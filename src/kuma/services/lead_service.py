"""潜客意向（`commerce.lead`）——`Task-Server.md` §18.3 / `Task-Plan.md` §9.1 角色 A。

**这张表存在的意义是"能被跟进"**。一条意向如果落不进某个经销商的看板，
它就只是数据库里的一行垃圾——所以本服务里最重要的两件事不是校验，而是：

1. **分派**（`_assign`）：用户自选门店 → 直接派给它；没自选但带了坐标 → 就近派；
   两者都没有 → 留 `assigned_dealer_org_id = NULL`，进平台的"未分派"队列（`unassigned=true`）。
   未分派对经销商**不可见**（`lead_repo` 模块注释），这是有意的：没派就是没主。
2. **状态只能由跟进方改**：本人只能看自己的；经销商只能改**派到自家**的；
   重新分派只有平台能做（经销商能改派给别人 = 能捞别家线索）。

## 两个写入口，一套落库

- `POST /leads`（登录）：`account_id` 由服务端填，**地区取 `core.account.region_id`**，
  不接受前端传 region——否则一个人能给任意地区灌线索。
- `POST /public/leads`（portal 未登录）：无 principal，`region_id` 与 `contact_phone`
  必填；限流在中间件（`api/app.py` 的 `/api/v1/public/leads` = 10/min/IP，
  比公开读的 120/min 严）。库里 `uq_lead_fresh_phone` 兜重复提交。

## 状态机（比 §5.2 的两条订单线简单，因为这里没有钱）

`new → contacted → visited → won | lost`，另允许 `lost → contacted`（客户半年后回来是常态）。
`won` 是终态：真要成交必须走订单（M8/M9），在 lead 上点"成交"不产生任何业务后果，
所以也不允许从 `won` 回退——想改就重新登记一条，留下两条记录比篡改一条诚实。

## PII

`contact_name` / `contact_phone` 是个人信息。未登录线索不挂账号，因此不在
`gdpr_service` 的按账号导出/删除范围内——**保留期与删除口径属合规要定的事**
（`Task-Plan.md` §13 合规），代码里先不做自动清理，但别把它当可以永久留存的营销资产。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from ..core.scope import Principal
from ..repositories.lead_repo import LeadRepo
from ..schemas.commerce import LeadContext

#: 状态迁移表（键 → 允许的目标态）。`won` 无出边：成交要走订单。
LEAD_TRANSITIONS: dict[str, set[str]] = {
    "new": {"contacted", "lost"},
    "contacted": {"visited", "lost"},
    "visited": {"won", "lost"},
    "won": set(),
    "lost": {"contacted"},           # 复活：客户过一阵子回来
}


class LeadService:
    repo = LeadRepo()

    # ── 写 ────────────────────────────────────────────────────────────────────
    async def create_for_account(self, session: AsyncSession, principal: Principal, *,
                                 contact_name: str, contact_phone: str | None,
                                 intent_type_code: str | None,
                                 intent_dealer_org_id: int | None, note: str | None,
                                 context: LeadContext | None, ip: str | None) -> dict[str, Any]:
        """登录用户登记意向（`lead:write`）。"""
        principal.require("lead", "write")
        region_id = await self.repo.account_region(session, principal.account_id)
        if not region_id:
            # 账号没有地区就没法归到任何看板；这属于数据缺陷，不是用户错误
            raise ValidationFailed("账号缺少地区，无法登记意向", code="lead_account_region_missing")
        return await self._insert(
            session, account_id=principal.account_id, region_id=str(region_id),
            contact_name=contact_name, contact_phone=contact_phone,
            intent_type_code=intent_type_code, intent_dealer_org_id=intent_dealer_org_id,
            note=note, context=context, source="app", actor=principal, ip=ip)

    async def create_anonymous(self, session: AsyncSession, *, region_id: str,
                               contact_name: str, contact_phone: str,
                               intent_type_code: str | None,
                               intent_dealer_org_id: int | None, note: str | None,
                               context: LeadContext | None, ip: str | None) -> dict[str, Any]:
        """portal 未登录登记意向（无 principal，靠中间件限流 + 库级去重）。"""
        if not contact_phone.strip():
            raise ValidationFailed("未登录提交必须留联系电话", code="lead_contact_required")
        return await self._insert(
            session, account_id=None, region_id=region_id, contact_name=contact_name,
            contact_phone=contact_phone, intent_type_code=intent_type_code,
            intent_dealer_org_id=intent_dealer_org_id, note=note, context=context,
            source="portal", actor=None, ip=ip)

    async def _insert(self, session: AsyncSession, *, account_id: int | None,
                      region_id: str, contact_name: str, contact_phone: str | None,
                      intent_type_code: str | None, intent_dealer_org_id: int | None,
                      note: str | None, context: LeadContext | None, source: str,
                      actor: Principal | None, ip: str | None) -> dict[str, Any]:
        name = contact_name.strip()
        if not name:
            raise ValidationFailed("联系人姓名不能为空", code="lead_contact_required")
        phone = contact_phone.strip() if contact_phone else None

        # 先查再插是为了给前端一个**明确原因**（"这条已经在跟进"），
        # 而不是撞 `uq_lead_fresh_phone` 后被全局处理器翻成笼统的 `duplicate`。
        # 两者之间仍有竞态：真撞约束时全局处理器兜成 409 duplicate，不会 500。
        if phone:
            dup = await self.repo.recent_duplicate(session, contact_phone=phone,
                                                   intent_type_code=intent_type_code)
            if dup:
                raise Conflict("这条意向已登记，正在跟进中", code="lead_duplicate",
                               detail={"lead_no": dup["lead_no"],
                                       "created_at": str(dup["created_at"])})

        assigned = await self._assign(session, region_id=region_id,
                                      intent_dealer_org_id=intent_dealer_org_id,
                                      context=context)
        payload = self._payload(context)
        lead = await self.repo.insert(
            session, lead_no=self._no("LD"), account_id=account_id, region_id=region_id,
            contact_name=name, contact_phone=phone, intent_type_code=intent_type_code,
            intent_dealer_org_id=intent_dealer_org_id, assigned_dealer_org_id=assigned,
            source=source, note=note, payload=payload)
        await write_audit(session, actor=actor, action="lead.create",
                          resource_type="lead", resource_id=lead["id"],
                          after={"lead_no": lead["lead_no"], "region_id": region_id,
                                 "source": source, "account_id": account_id,
                                 "intent_type_code": intent_type_code,
                                 "intent_dealer_org_id": intent_dealer_org_id,
                                 "assigned_dealer_org_id": assigned},
                          ip=ip)
        return lead

    async def _assign(self, session: AsyncSession, *, region_id: str,
                      intent_dealer_org_id: int | None,
                      context: LeadContext | None) -> int | None:
        """分派结果（可为 None = 进平台未分派队列）。

        自选门店必须是**在职经销商**：派给停用或不是 dealer 的 org，
        线索就再也没人能跟进（`assigned_dealer_org_id` 是经销商看板的唯一入口）。
        """
        if intent_dealer_org_id is not None:
            dealer = await self.repo.active_dealer(session, intent_dealer_org_id)
            if dealer is None:
                raise ValidationFailed("所选门店不是在职经销商", code="lead_dealer_unknown")
            return int(dealer["id"])
        if context and context.lat is not None and context.lng is not None:
            near = await self.repo.nearest_dealer(session, region_id=region_id,
                                                  lat=context.lat, lng=context.lng)
            return int(near["id"]) if near else None
        return None

    # ── 读 ────────────────────────────────────────────────────────────────────
    async def page(self, session: AsyncSession, principal: Principal, *, status: str | None,
                   region_id: str | None, unassigned: bool, q: str | None,
                   limit: int, offset: int) -> dict[str, Any]:
        """同一个 `GET /leads`，按**当前 active_identity** 决定看哪些。

        用 active_identity 而不是"持有哪个身份"：同一个人既可能是消费者又可能是经销商员工，
        切到 consumer 时就该只看自己的意向（与 `Principal.scope_kind()` 同一口径）。
        """
        principal.require("lead", "read")
        owner_account_id: int | None = None
        dealer_org_ids: list[int] | None = None
        if principal.is_platform:
            pass                                   # 平台看全量
        elif principal.active_identity == "dealer_staff":
            dealer_org_ids = principal.org_ids("dealer")
        else:
            owner_account_id = principal.account_id
        if unassigned and not principal.is_platform:
            raise Forbidden("只有平台能看未分派队列", code="lead_unassigned_platform_only")
        items, total = await self.repo.page(session, owner_account_id=owner_account_id,
                                            dealer_org_ids=dealer_org_ids, status=status,
                                            region_id=region_id, unassigned=unassigned, q=q,
                                            limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": max(1, min(limit, 200)),
                "offset": max(0, offset)}

    async def detail(self, session: AsyncSession, principal: Principal,
                     lead_id: int) -> dict[str, Any]:
        principal.require("lead", "read")
        lead = await self._visible(session, principal, lead_id)
        return lead

    # ── 状态流转（跟进侧）─────────────────────────────────────────────────────
    async def set_status(self, session: AsyncSession, principal: Principal, lead_id: int, *,
                         status: str, note: str | None, assign_dealer_org_id: int | None,
                         ip: str | None) -> dict[str, Any]:
        principal.require("lead", "write")
        lead = await self._visible(session, principal, lead_id, write=True)

        if assign_dealer_org_id is not None:
            if not principal.is_platform:
                raise Forbidden("只有平台能重新分派", code="lead_reassign_platform_only")
            if await self.repo.active_dealer(session, assign_dealer_org_id) is None:
                raise ValidationFailed("分派目标不是在职经销商", code="lead_dealer_unknown")
            moved = await self.repo.reassign(session, lead_id=lead_id,
                                             dealer_org_id=assign_dealer_org_id)
            if moved is None:
                raise NotFound("意向不存在", code="lead_not_found")
            await write_audit(session, actor=principal, action="lead.reassign",
                              resource_type="lead", resource_id=lead_id,
                              before={"assigned_dealer_org_id": lead["assigned_dealer_org_id"]},
                              after={"assigned_dealer_org_id": assign_dealer_org_id}, ip=ip)
            lead = moved

        current = str(lead["status"])
        if status != current and status not in LEAD_TRANSITIONS.get(current, set()):
            raise Conflict(f"状态不能从 {current} 改到 {status}", code="lead_transition_denied",
                           detail={"from": current, "to": status,
                                   "allowed": sorted(LEAD_TRANSITIONS.get(current, set()))})

        row = await self.repo.set_status(session, lead_id=lead_id, status=status,
                                         followed_by=principal.account_id, note=note)
        if row is None:
            raise NotFound("意向不存在", code="lead_not_found")
        await write_audit(session, actor=principal, action="lead.status",
                          resource_type="lead", resource_id=lead_id,
                          before={"status": current},
                          after={"status": row["status"],
                                 "followed_by": row["followed_by"], "note": note}, ip=ip)
        return row

    # ── 内部 ──────────────────────────────────────────────────────────────────
    async def _visible(self, session: AsyncSession, principal: Principal, lead_id: int, *,
                       write: bool = False) -> dict[str, Any]:
        """单条可见性：本人看自己的；经销商看派到自家的；平台看全部。"""
        lead = await self.repo.by_id(session, lead_id)
        if lead is None:
            raise NotFound("意向不存在", code="lead_not_found")
        if principal.is_platform:
            return lead
        if principal.active_identity == "dealer_staff" or (
                write and "dealer_staff" in principal.identities):
            if int(lead["assigned_dealer_org_id"] or 0) in set(principal.org_ids("dealer")):
                return lead
            raise Forbidden("这条线索不在你家名下", code="lead_out_of_scope")
        if int(lead["account_id"] or 0) == principal.account_id:
            return lead
        raise Forbidden("只能查看自己登记的意向", code="lead_out_of_scope")

    @staticmethod
    def _payload(context: LeadContext | None) -> dict[str, Any]:
        if context is None:
            return {}
        return {k: v for k, v in context.model_dump().items() if v is not None}

    @staticmethod
    def _no(prefix: str) -> str:
        return f"{prefix}{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6]}"
