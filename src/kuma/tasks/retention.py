"""保留策略：原始遥测按天分区保留 N 天，清理 = DROP PARTITION（§5.3 禁 DELETE）。

同时预建未来分区（不装 pg_partman，自己建）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import text

from ..core.config import get_settings
from ..core.db import get_maintenance_engine
from ..core.logging import get_logger
from ..core.redis_client import acquire_lock, release_lock

log = get_logger("tasks.retention")

# 保留天数按环境配（KUMA_TELE_KEEP_DAYS）：调试机 20G 盘按 1000 台 @5s ≈ 5GB/天，默认只留 3 天；
# 正式服务器把 KUMA_TELE_KEEP_DAYS=7 即可，不改代码


async def ensure_partitions() -> None:
    token = await acquire_lock("retention_partitions", ttl=600)
    if not token:
        return
    try:
        # 分区是 DDL：kuma_app 无权限，走维护通道（kuma_owner）
        engine = get_maintenance_engine()
        today = datetime.now(UTC).date()
        async with engine.begin() as conn:
            cfg = get_settings()
            for offset in range(cfg.tele_precreate_days + 1):
                day = today + timedelta(days=offset)
                nxt = day + timedelta(days=1)
                name = f"telemetry_{day:%Y%m%d}"
                exists = (await conn.execute(text(
                    "SELECT 1 FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = :n AND n.nspname = 'tele'"), {"n": name})).first()
                if not exists:
                    await conn.execute(text(
                        f"CREATE TABLE IF NOT EXISTS tele.{name} PARTITION OF tele.telemetry "
                        f"FOR VALUES FROM ('{day}') TO ('{nxt}')"
                    ))
                    log.info("retention.partition_created", table=name)

            rows = (await conn.execute(text("""
                SELECT c.relname FROM pg_class c
                JOIN pg_inherits i ON i.inhrelid = c.oid
                WHERE i.inhparent = 'tele.telemetry'::regclass
            """))).scalars().all()
            cutoff = today - timedelta(days=cfg.tele_keep_days)
            for name in rows:
                d = _parse(name)
                if d and d < cutoff:
                    await conn.execute(text(f"DROP TABLE IF EXISTS tele.{name}"))
                    log.info("retention.partition_dropped", table=name, cutoff=str(cutoff))
    finally:
        await release_lock(token)


async def refresh_rep_views() -> None:
    from ..repositories.telemetry_repo import TelemetryRepo

    await TelemetryRepo().refresh_hourly()


def _parse(name: str) -> date | None:
    try:
        return datetime.strptime(name.split("_")[1], "%Y%m%d").date()
    except (IndexError, ValueError):
        return None
