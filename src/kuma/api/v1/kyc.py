"""KYC 路由：本人提交/查看（`/me/kyc`）+ 平台审核（`/kyc/*`）。

拆两个前缀是因为权限完全不同：`/me/kyc` 用 `kyc:write`（人人能提交自己的），
`/kyc/queue`、`/kyc/{id}/review` 用 `kyc:review`（只有平台，且属于 DUAL_CONTROL）。
业务判断全在 `services/kyc_service.py`（§7 铁律 3：services 不碰 HTTP，api 不写 SQL）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...schemas.kyc import KycReviewIn, KycSubmitIn
from ...services.kyc_service import KycService

me_router = APIRouter(prefix="/me", tags=["kyc"])
router = APIRouter(prefix="/kyc", tags=["kyc"])
svc = KycService()


@me_router.get("/kyc")
async def my_kyc(session: Db, principal: PrincipalDep,
                 limit: int = Query(default=50, ge=1, le=200),
                 offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """我的实名记录 + 当前等级（积分页显示"未实名"时要能给原因）。"""
    return await svc.my_records(session, principal, limit=limit, offset=offset)


@me_router.post("/kyc", status_code=201)
async def submit_kyc(body: KycSubmitIn, request: Request, session: Db,
                     principal: PrincipalDep) -> dict[str, Any]:
    """提交实名认证。证件照片先经 `POST /media`（owner_type=account）再带 media_ids。"""
    return await svc.submit(session, principal, level=body.level, id_type=body.id_type,
                            id_number=body.id_number, media_ids=body.media_ids,
                            note=body.note, ip=client_ip(request))


@router.get("/queue")
async def kyc_queue(session: Db, principal: PrincipalDep,
                    status: str = Query(default="pending"),
                    region_id: str | None = Query(default=None),
                    level: int | None = Query(default=None, ge=1, le=3),
                    limit: int = Query(default=50, ge=1, le=200),
                    offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """待审核队列（kyc:review）。带 total，先到先审。"""
    return await svc.queue(session, principal, status=status, region_id=region_id, level=level,
                           limit=limit, offset=offset)


@router.post("/{kyc_id}/review")
async def review_kyc(kyc_id: int, body: KycReviewIn, request: Request, session: Db,
                     principal: PrincipalDep) -> dict[str, Any]:
    """审核（双人）。通过 = 抬 core.account.kyc_level；驳回必须给 reason。"""
    return await svc.review(session, principal, kyc_id=kyc_id, approve=body.approve,
                            co_approver_account_id=body.co_approver_account_id,
                            reason=body.reason, ip=client_ip(request))
