"""审计读取（C7，2026-10-22 补）。

写入侧一直都有（`core/audit.py`，12+ 处调用），但**没有任何读取端点**——
`R_AUDIT` 权限位定义了却零使用，等于"记了但没人能看"，M13 的"监管可核查"落不了地。

表按月分区，索引只有三个（`008_audit_outbox.sql:21-23`）：
`(resource_type,resource_id,created_at DESC)`、`(actor_account_id,created_at DESC)`、`query` 的 GIN。
所以查询**必须带时间窗**，且不要按 `action` 前缀模糊查（无索引，会全分区扫）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select, text

from ..core.pagination import PageParams
from ..models.audit import AuditLog


class AuditRepo:
    def page(self, *, actor_account_id: int | None = None, action: str | None = None,
             resource_type: str | None = None, resource_id: str | int | None = None,
             since: datetime | None = None, until: datetime | None = None,
             page: PageParams | None = None) -> Select:
        stmt = select(AuditLog)
        if actor_account_id is not None:
            stmt = stmt.where(AuditLog.actor_account_id == actor_account_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)          # 精确匹配，走得到索引
        if resource_type:
            stmt = stmt.where(AuditLog.resource_type == resource_type)
        if resource_id is not None:
            stmt = stmt.where(AuditLog.resource_id == str(resource_id))
        if since:
            stmt = stmt.where(AuditLog.created_at >= since)
        if until:
            stmt = stmt.where(AuditLog.created_at < until)
        p = page or PageParams()
        return stmt.order_by(AuditLog.created_at.desc()).limit(p.limit).offset(p.offset)

    def actions(self, *, since: datetime, until: datetime | None = None) -> Select:
        """有哪些 action（前端筛选下拉的数据源，避免把 action 名单写死在前端）。"""
        conds = [AuditLog.created_at >= since]
        if until:
            conds.append(AuditLog.created_at < until)
        return (select(AuditLog.action, func.count().label("n"))
                .where(*conds).group_by(AuditLog.action)
                .order_by(text("n DESC")).limit(200))
