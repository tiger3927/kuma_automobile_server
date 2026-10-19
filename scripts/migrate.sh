#!/usr/bin/env bash
# Alembic 迁移（DDL 只用 kuma_owner；运行时 kuma_app 无 DDL 权限）
set -uo pipefail
cd /data/kuma_automobile_server
set -a; . deploy/.env; set +a
export KUMA_ENV=dev
export KUMA_DSN="postgresql+asyncpg://kuma_app:${KUMA_APP_PASSWORD}@127.0.0.1:5432/kuma"
export KUMA_MIGRATE_DSN="postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
BIN=/data/tiger/miniconda3/envs/kuma/bin
$BIN/alembic upgrade head 2>&1 | tail -6
$BIN/alembic current 2>&1 | tail -2
