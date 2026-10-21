"""数据主体权利路由：`/me/data`（导出）与 `/me/erasure`（匿名化）。

权限只用 `self`（§10.1 矩阵：`regulator` 对 self 无权限、`platform_staff` 只读）——
**平台员工不能通过这里删自己的账号**，那类操作走内部流程，避免"自己把自己抹掉"绕过审计。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import ORJSONResponse

from ...core.deps import Db, IdempotencyDep, PrincipalDep, client_ip
from ...core.exceptions import ValidationFailed
from ...schemas.media import ErasureIn
from ...services.gdpr_service import gdpr_service

router = APIRouter(prefix="/me", tags=["gdpr"])


@router.get("/data")
async def export_my_data(session: Db, principal: PrincipalDep,
                         download: bool = False) -> Response:
    """访问权 / 可携权：导出与本人相关的数据（口径见 `services/gdpr_service.py`）。

    `?download=1` 加 `Content-Disposition`，浏览器直接存文件；默认内联返回便于前端展示。
    """
    principal.require("self", "read")
    payload = await gdpr_service.export_self(session, principal)
    headers = {}
    if download:
        account_id = payload["account_id"]
        headers["Content-Disposition"] = f'attachment; filename="kuma-data-{account_id}.json"'
    return ORJSONResponse(payload, headers=headers)


@router.post("/erasure")
async def erase_my_data(body: ErasureIn, request: Request, session: Db,
                        principal: PrincipalDep, _idem: IdempotencyDep) -> dict:
    """删除权 → **匿名化**（不物理删行）。必须 `confirm=true`。"""
    if not body.confirm:
        raise ValidationFailed("必须显式 confirm=true：该操作不可逆")
    principal.require("self", "write")
    return await gdpr_service.erase_self(session, principal, reason=body.reason,
                                          ip=client_ip(request))
