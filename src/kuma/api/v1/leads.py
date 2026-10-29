"""潜客意向路由（`commerce.lead`）。

一个前缀、三种视图（本人 / 经销商 / 平台）由 services 按 `active_identity` 决定，
**不拆成 `/me/leads` 与 `/admin/leads` 两套路径**：同一份数据、同一套可见性规则，
拆两套路径就会有两套"谁能看什么"的实现，迟早对不上（`Task-Server.md` §7 铁律 2：
一个事实只有一个写入口与一个读入口）。

未登录的入口不在这里，在 `api/v1/public.py` 的 `POST /public/leads`——
那里没有 `PrincipalDep`（官网登录前就要能提交），安全边界换成
中间件限流（`/api/v1/public/leads` = 10/min/IP）+ 库级去重。

`status` 查询参数用 `LeadStatus`（Literal）而不是裸 `str`：FastAPI 会在进业务前
把非法值挡成 422。裸 `str` 的教训在积分核销队列（`purchase_service.py:59-64` 的注释）——
`?status=anything` 会原样进 SQL 绑定，参数化不会注入，但会**静默返回空列表**，
让跟进人以为"没活干"。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep, client_ip
from ...schemas.commerce import LeadIn, LeadStatus, LeadStatusIn
from ...services.lead_service import LeadService

router = APIRouter(prefix="/leads", tags=["lead"])
svc = LeadService()


@router.get("")
async def list_leads(session: Db, principal: PrincipalDep,
                     status: LeadStatus | None = Query(default=None),
                     region_id: str | None = Query(default=None, max_length=2),
                     unassigned: bool = Query(default=False, description="只看未分派（平台）"),
                     q: str | None = Query(default=None, max_length=64),
                     limit: int = Query(default=20, ge=1, le=200),
                     offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """意向列表：本人看自己的，经销商看派到自家的，平台看全量。"""
    return await svc.page(session, principal, status=status, region_id=region_id,
                          unassigned=unassigned, q=q, limit=limit, offset=offset)


@router.post("", status_code=201)
async def create_lead(body: LeadIn, request: Request, session: Db, principal: PrincipalDep,
                      _idem: IdempotencyDep) -> dict[str, Any]:
    """登记意向（登录用户）。地区取账号自己的地区，不接受前端传。"""
    return await svc.create_for_account(
        session, principal, contact_name=body.contact_name, contact_phone=body.contact_phone,
        intent_type_code=body.intent_type_code, intent_dealer_org_id=body.intent_dealer_org_id,
        note=body.note, context=body.context, ip=client_ip(request))


@router.get("/{lead_id}")
async def lead_detail(lead_id: int, session: Db, principal: PrincipalDep) -> dict[str, Any]:
    return await svc.detail(session, principal, lead_id)


@router.post("/{lead_id}/status")
async def lead_status(lead_id: int, body: LeadStatusIn, request: Request, session: Db,
                      principal: PrincipalDep) -> dict[str, Any]:
    """跟进：改状态（经销商只能改派到自家的）；`assign_dealer_org_id` 只有平台能带。"""
    return await svc.set_status(session, principal, lead_id, status=body.status,
                                note=body.note, assign_dealer_org_id=body.assign_dealer_org_id,
                                ip=client_ip(request))
