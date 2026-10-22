"""数据库访问基座（SQLAlchemy 2.0 async + asyncpg）。

- 连接池按 Task-DataBase.md §8：api 5+5、获取超时 5s；worker 会话内把 statement_timeout 抬到 300s。
- 运行时账号固定 `kuma_app`（只 DML）；DDL 只由 Alembic 用 kuma_owner 执行。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings
from .logging import get_logger

log = get_logger("db")

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_maint_engine: AsyncEngine | None = None


def _json_default(o: object) -> object:
    """JSONB 序列化的兜底转换。

    **2026-10-22 实测踩到的坑（值得记住）**：`write_audit(after={"snooze_until": <datetime>})`
    让 `json.dumps` 抛 `TypeError: Object of type datetime is not JSON serializable`。
    因为审计写入没有 flush，异常发生在 FastAPI 依赖 teardown 的 `commit()` 阶段——
    **响应已经 200 发给客户端，事务却整个回滚**，业务写入静默丢失。
    审计是合规硬要求，这种"客户端以为成功、库里什么都没有"的失败方式最危险。

    所以：① 这里把 datetime/date/Decimal/UUID/bytes 统一转成 JSON 可表达的形式；
    ② `write_audit` 改为写入后立即 flush，让序列化问题在响应发出**之前**暴露。
    """
    if isinstance(o, datetime | date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, bytes | bytearray):
        return o.decode("utf-8", "replace")
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"JSONB 无法序列化 {type(o).__name__}")


def _apply_session_config(dbapi_conn, _record, settings: Settings, app_suffix: str = "") -> None:
    """每个物理连接建立时设定会话参数（全链路 UTC + 可追踪的 application_name）。"""
    with dbapi_conn.cursor() as cur:
        cur.execute("SET timezone = 'UTC'")
        cur.execute(f"SET application_name = 'kuma-{settings.role}{app_suffix}'")
        if settings.role == "worker":
            # kuma_app 角色级是 30s；聚合类任务在此会话内抬到 300s（Task-DataBase.md §8）
            cur.execute("SET statement_timeout = 300000")
        else:
            cur.execute("SET statement_timeout = 30000")


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    s = settings or get_settings()
    _engine = create_async_engine(
        s.dsn,
        pool_size=s.pool_size,
        max_overflow=s.max_overflow,
        pool_timeout=s.pool_timeout,
        pool_pre_ping=True,
        future=True,
        # 见 `_json_default` 的踩坑说明：所有 JSONB 列（audit.before/after/query、
        # alarm.payload、command.payload…）都走这个序列化器。
        json_serializer=lambda o: json.dumps(o, default=_json_default, ensure_ascii=False),
    )
    event.listen(_engine.sync_engine, "connect", lambda c, r: _apply_session_config(c, r, s))
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_maintenance_engine(settings: Settings | None = None) -> AsyncEngine:
    """DDL / 维护通道（分区创建与 DROP、rep 物化视图 REFRESH）。

    `kuma_app` 只有 DML 权限：分区 DDL 会 `permission denied for schema tele`，
    `REFRESH MATERIALIZED VIEW` 只有物化视图 owner 能干（PG 无 GRANT REFRESH）。
    所以这类动作走 `KUMA_MAINTENANCE_DSN`（kuma_owner），小池、只给 worker 用。
    """
    global _maint_engine
    if _maint_engine is not None:
        return _maint_engine
    s = settings or get_settings()
    if not s.maintenance_dsn:
        log.warning("db.maintenance_dsn_missing", hint="分区 DDL 与 rep 刷新会因权限失败")
    _maint_engine = create_async_engine(
        s.maintenance_dsn or s.dsn,
        pool_size=1,
        max_overflow=0,
        pool_timeout=s.pool_timeout,
        pool_pre_ping=True,
        future=True,
    )
    event.listen(_maint_engine.sync_engine, "connect",
                 lambda c, r: _apply_session_config(c, r, s, ":maint"))
    return _maint_engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖与脚本通用；异常回滚、正常提交。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def tx() -> AsyncIterator[AsyncSession]:
    """services 层显式事务边界（跨域写 + outbox 必须同事务）。"""
    factory = get_session_factory()
    async with factory() as session, session.begin():
        yield session


async def raw_pg(settings: Settings | None = None):
    """取 asyncpg 原生连接（只给 telemetry_repo 的 COPY 用，见 §7 铁律 4）。"""
    engine = get_engine(settings)
    conn = await engine.connect()
    driver = await conn.get_raw_connection()
    return conn, driver.driver_connection  # asyncpg.Connection


async def healthcheck() -> dict[str, str]:
    try:
        factory = get_session_factory()
        async with factory() as s:
            # AsyncSession 没有 exec_driver_sql（实测），用 text() 即可
            await s.execute(text("select 1"))
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": type(exc).__name__}


async def dispose_engine() -> None:
    global _engine, _session_factory, _maint_engine
    if _engine is not None:
        await _engine.dispose()
    if _maint_engine is not None:
        await _maint_engine.dispose()
    _engine = None
    _session_factory = None
    _maint_engine = None
