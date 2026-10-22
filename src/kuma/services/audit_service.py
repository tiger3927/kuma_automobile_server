"""审计查询服务（C7）。

两条硬约束：
1. **只有 `R_AUDIT` 能读**（矩阵里 = `platform_staff` / `regulator`，只读，见 `core/scope.py:96`）；
2. **查审计这件事本身必须留痕**——否则"谁看过监管记录"就成了新的盲区。
   留痕写的是同一条 append-only 表，`action="audit.query"`、`query` 存本次过滤条件。

时间窗默认 7 天、上限 31 天：表按月分区且这台机器只有 512MB PG，
不带窗的审计查询等于全分区扫。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import ValidationFailed
from ..core.pagination import PageParams
from ..core.scope import R_AUDIT, Principal
from ..models.audit import AuditLog
from ..repositories.audit_repo import AuditRepo
from ..repositories.base import count_of

MAX_WINDOW_DAYS = 31
DEFAULT_WINDOW_DAYS = 7


class AuditService:
    repo = AuditRepo()

    async def page(self, session: AsyncSession, principal: Principal, *,
                   actor_account_id: int | None = None, action: str | None = None,
                   resource_type: str | None = None, resource_id: str | None = None,
                   since: datetime | None = None, until: datetime | None = None,
                   page: PageParams | None = None, ip: str | None = None) -> dict:
        principal.require(R_AUDIT, "read")
        since, until = self._window(since, until)

        stmt = self.repo.page(actor_account_id=actor_account_id, action=action,
                              resource_type=resource_type, resource_id=resource_id,
                              since=since, until=until, page=page)
        rows = (await session.execute(stmt)).scalars().all()
        total = await count_of(session, self.repo.page(
            actor_account_id=actor_account_id, action=action, resource_type=resource_type,
            resource_id=resource_id, since=since, until=until))

        params: dict[str, Any] = {"actor_account_id": actor_account_id, "action": action,
                                  "resource_type": resource_type, "resource_id": resource_id,
                                  "since": since, "until": until,
                                  "limit": (page or PageParams()).limit,
                                  "offset": (page or PageParams()).offset}
        await write_audit(session, actor=principal, action="audit.query",
                          resource_type="audit_log", query=params, ip=ip)

        return {"items": [{"created_at": a.created_at, "actor_account_id": a.actor_account_id,
                           "actor_identity": a.actor_identity, "action": a.action,
                           "resource_type": a.resource_type, "resource_id": a.resource_id,
                           "before": a.before, "after": a.after, "query": a.query,
                           "ip": str(a.ip) if a.ip else None} for a in rows],
                "total": total, "window": {"since": since, "until": until},
                "limit": (page or PageParams()).limit, "offset": (page or PageParams()).offset}

    async def actions(self, session: AsyncSession, principal: Principal, *,
                      since: datetime | None = None, until: datetime | None = None) -> dict:
        principal.require(R_AUDIT, "read")
        since, until = self._window(since, until)
        rows = (await session.execute(self.repo.actions(since=since, until=until))).all()
        return {"items": [{"action": r.action, "n": r.n} for r in rows],
                "window": {"since": since, "until": until}}

    async def recent_for_resource(self, session: AsyncSession, principal: Principal,
                                  resource_type: str, resource_id: str) -> list[dict]:
        """给别的端点复用：某条资源的审计轨迹（如告警详情里的"谁改过状态"）。"""
        principal.require(R_AUDIT, "read")
        since = datetime.now(UTC) - timedelta(days=DEFAULT_WINDOW_DAYS)
        rows = (await session.execute(
            select(AuditLog).where(AuditLog.resource_type == resource_type,
                                   AuditLog.resource_id == str(resource_id),
                                   AuditLog.created_at >= since)
            .order_by(AuditLog.created_at.desc()).limit(50)
        )).scalars().all()
        return [{"created_at": a.created_at, "actor_account_id": a.actor_account_id,
                 "actor_identity": a.actor_identity, "action": a.action} for a in rows]

    def _window(self, since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
        until = until or datetime.now(UTC)
        since = since or until - timedelta(days=DEFAULT_WINDOW_DAYS)
        if since >= until:
            raise ValidationFailed("since 必须早于 until")
        if until - since > timedelta(days=MAX_WINDOW_DAYS):
            raise ValidationFailed(f"审计查询窗口不得超过 {MAX_WINDOW_DAYS} 天")
        return since, until
