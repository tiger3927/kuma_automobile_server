"""审计写入：与业务写同事务，append-only（Task-Plan.md §10-7）。

监管（`all_readonly`）每次访问必须把**查询条件**写进 `query` 字段（§9.6）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import AuditLog
from .scope import Principal


async def write_audit(
    session: AsyncSession,
    *,
    actor: Principal | None,
    action: str,
    resource_type: str,
    resource_id: str | int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    session.add(AuditLog(
        actor_account_id=actor.account_id if actor else None,
        actor_identity=actor.active_identity if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        before=before,
        after=after,
        query=query,
        ip=ip,
    ))


async def audit_regulator_query(
    session: AsyncSession, *, actor: Principal, action: str,
    resource_type: str, params: dict[str, Any], ip: str | None,
) -> None:
    """监管读操作也要留痕（含查询条件）。"""
    await write_audit(
        session, actor=actor, action=action, resource_type=resource_type,
        resource_id=None, query=_jsonable(params), ip=ip,
    )


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
