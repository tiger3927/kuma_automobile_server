"""审计写入：与业务写同事务，append-only（Task-Plan.md §10-7）。

监管（`all_readonly`）每次访问必须把**查询条件**写进 `query` 字段（§9.6）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
        # 三个 JSONB 列都过 _jsonable：调用方经常把 datetime / UUID 直接塞进 before/after
        # （例如 `after={"snooze_until": <datetime>}`），不转换会在 commit 时炸。
        before=_jsonable(before) if before is not None else None,
        after=_jsonable(after) if after is not None else None,
        query=_jsonable(query) if query is not None else None,
        ip=ip,
    ))
    # **必须 flush**：审计与业务写同事务，若不立即写盘，序列化/约束异常会发生在
    # FastAPI 依赖 teardown 的 commit 阶段——那时响应已经 200 发给客户端，
    # 事务却整体回滚，业务写入静默丢失（2026-10-22 实测就是这个形态）。
    # flush 让失败在响应发出之前抛出，客户端拿到 500 而不是假成功。
    await session.flush()


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
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)
