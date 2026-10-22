#!/usr/bin/env bash
set -uo pipefail
cd /data/kuma_automobile_server
set -a; . deploy/.env; set +a
export KUMA_ENV=dev
export KUMA_DSN="postgresql+asyncpg://kuma_app:${KUMA_APP_PASSWORD}@127.0.0.1:5432/kuma"
export KUMA_MAINTENANCE_DSN="postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
export KUMA_AMQP_URL="amqp://kuma_mq:${KUMA_MQ_PASSWORD}@127.0.0.1:5672/%2Fkuma"
# kuma-redis 独立容器（6380 + requirepass，2026-10-22 §4.1）：不注入带口令 URL 时
# 默认值 redis://127.0.0.1:6380/0 无口令 → NOAUTH，healthz/缓存用例必挂
export KUMA_REDIS_URL="redis://:${KUMA_REDIS_PASSWORD}@127.0.0.1:6380/0"
export KUMA_MQTT_URL="mqtt://127.0.0.1:1883"
export KUMA_MQTT_USER=kuma_mq
export KUMA_MQTT_PASSWORD="${KUMA_MQ_PASSWORD}"
export KUMA_JWT_SECRET="verify-only-secret-please-rotate-32b"
PY=/data/tiger/miniconda3/envs/kuma/bin/python

# 注意：pyproject 的 addopts 已带 -q，这里再加 -q 会变成 -qq，把"N passed"汇总行也吞掉
echo "### unit"
$PY -m pytest -m "not db and not mq" --tb=short 2>&1 | tail -6
echo "### integration (db+mq)"
$PY -m pytest -m db --tb=short 2>&1 | tail -14
