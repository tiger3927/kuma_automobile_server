"""Alembic 环境（异步）。

- 迁移账号必须是 `kuma_owner`（`KUMA_MIGRATE_DSN`），运行时账号 `kuma_app` 无 DDL 权限；
- 结构基线由 `migrations/sql/001`–`014` 建立，M1 用 `alembic stamp head` 认领，
  此后**任何结构改动一律走迁移**（Task-DataBase.md §7）。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.kuma.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

dsn = os.getenv("KUMA_MIGRATE_DSN")
if not dsn:
    raise SystemExit("迁移必须提供 KUMA_MIGRATE_DSN（kuma_owner，运行时 kuma_app 无 DDL 权限）")
config.set_main_option("sqlalchemy.url", dsn)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=dsn, target_metadata=target_metadata, literal_binds=True,
                      compare_type=True, schema_translate_map=None)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata,
                      compare_type=True, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = async_engine_from_config(config.get_section(config.config_ini_section, {}),
                                      prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
