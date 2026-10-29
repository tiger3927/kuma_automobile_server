"""公开端点（portal 官网：车型参数页、经销商查询，以及**意向登记**）。

**无鉴权**——安全边界靠三层：中间件限流（`api/app.py`：`/api/v1/public/` = 120/min/IP，
`/api/v1/public/leads` = **10/min/IP**，规则按插入顺序取第一个前缀匹配）、
本层不接收任何"取回内部字段"的参数、字段白名单在 services。
不要在这里加 PrincipalDep：官网在登录前就要能浏览。

`POST /public/leads` 是本层**唯一的写端点**（2026-10-29，guest 角色 A 的未登录阶段，
`Task-Web.md` §7.4 / `Task-Server.md` §18.3）。它必须存在，否则"潜在客户"这条链
在起点就断——官网流量无法归因，注册后才登记等于把绝大多数访客排除在外。
代价与防线：

- 限流比读端点严一个量级（10/min/IP），且按 `X-Forwarded-For` 分桶；
- 入参有界（`LeadAnonymousIn`：姓名/电话/地区必填，`context` 四个字段全部有界），
  **不接受自由 dict**；
- 库级 `uq_lead_fresh_phone` 挡同手机号 + 同车型的重复提交；
- 只写 `commerce.lead` 一张表，不碰订单、不碰支付、不返回任何内部字段（只回 lead_no 与状态）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...core.deps import Db, client_ip
from ...core.pagination import PageParams
from ...schemas.commerce import LeadAnonymousIn
from ...services.lead_service import LeadService
from ...services.public_service import PublicService

router = APIRouter(prefix="/public", tags=["public"])
svc = PublicService()
leads = LeadService()


@router.get("/vehicle-types")
async def vehicle_types(session: Db, region_id: str | None = Query(default=None, max_length=2),
                        product_line: str | None = None, category: str | None = None,
                        q: str | None = Query(default=None, max_length=64),
                        limit: int = 20, offset: int = 0) -> dict:
    return await svc.types(session, region_id=region_id, product_line=product_line,
                           category=category, q=q, page=PageParams(limit=limit, offset=offset))


@router.get("/vehicle-types/{type_code}")
async def vehicle_type_detail(type_code: str, session: Db) -> dict:
    return await svc.type_detail(session, type_code)


@router.get("/dealers")
async def dealers(session: Db, region_id: str | None = Query(default=None, max_length=2),
                  q: str | None = Query(default=None, max_length=64),
                  limit: int = 20, offset: int = 0) -> dict:
    return await svc.dealers(session, region_id=region_id, q=q,
                             page=PageParams(limit=limit, offset=offset))


@router.post("/leads", status_code=201)
async def submit_lead(body: LeadAnonymousIn, request: Request, session: Db) -> dict[str, Any]:
    """未登录登记意向（官网"我想了解 / 询价"入口）。

    响应**只回 lead_no 与状态**：公开端点不返回内部 id、不返回分派给了谁
    （门店名要显示的话，前端用 `GET /public/dealers` 自己查）。
    重复提交返回 409 `lead_duplicate`，超阈值 429 `rate_limited` + `retry-after`。
    """
    lead = await leads.create_anonymous(
        session, region_id=body.region_id, contact_name=body.contact_name,
        contact_phone=body.contact_phone, intent_type_code=body.intent_type_code,
        intent_dealer_org_id=body.intent_dealer_org_id, note=body.note,
        context=body.context, ip=client_ip(request))
    return {"lead_no": lead["lead_no"], "status": lead["status"]}
