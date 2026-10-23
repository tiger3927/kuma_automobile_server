"""双人授权（dual control）的统一校验（`Task-Plan.md` §9.5 / §10.1，`core/scope.py:120` DUAL_CONTROL）。

高危动作（改积分规则、核销收款凭证、退款、下行控制）必须**两个人签字**，
且第二个人不能是本人、必须真的有对应角色。为什么要在库里查而不是信前端传的 id：

- 只判"不等于本人"是假的二人——传一个不存在的 `co_approver_account_id=2` 也能过
  （积分抵扣上线时就是这个状态，测试里蒙混过关，实盘就是单人签字）；
- 审计里记的"谁联审"必须是可追责的真实账号，否则 §9.5 的双人授权在证据链上不成立。

`command_service._require_co_approver` 是同一件事的第一份实现（写在那儿、私有）。
新代码统一用这里；老代码不在本轮改动范围内（能跑就别动，避免波及车联网）。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import Forbidden
from ..core.scope import Principal
from ..models.core import Identity


async def require_co_approver(session: AsyncSession, principal: Principal,
                              co_approver_account_id: int | None, *,
                              role: str = "platform_staff") -> None:
    """校验联审人：必须给、不能是本人、且真的有 `role` 的 active 身份。"""
    if co_approver_account_id is None:
        raise Forbidden("dual_control_required")
    if co_approver_account_id == principal.account_id:
        raise Forbidden("co_approver_must_differ")
    ok = (await session.execute(
        Identity.__table__.select().where(
            Identity.account_id == co_approver_account_id,
            Identity.identity_type == role, Identity.status == "active")
    )).first()
    if ok is None:
        raise Forbidden(f"co_approver_not_{role}")
