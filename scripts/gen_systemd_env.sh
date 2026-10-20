#!/usr/bin/env bash
# 从 deploy/.env（只有口令）组合出 systemd 用的 deploy/runtime.env。
# systemd 的 EnvironmentFile 不做变量展开，DSN 必须在生成时拼好。
# 口令仍然只有 deploy/.env 一份来源（Debug.md 是唯一凭据台账）。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . deploy/.env; set +a

if ! grep -q '^KUMA_JWT_SECRET=' deploy/.env; then
  SECRET="$(head -c 32 /dev/urandom | base64 | tr -d '/+=')"
  echo "KUMA_JWT_SECRET=${SECRET}" >> deploy/.env
  echo "已生成并写入 deploy/.env 的 KUMA_JWT_SECRET"
fi
set -a; . deploy/.env; set +a

umask 077
OUT=deploy/runtime.env
{
  echo "KUMA_ENV=prod"
  echo "KUMA_DSN=postgresql+asyncpg://kuma_app:${KUMA_APP_PASSWORD}@127.0.0.1:5432/kuma"
  echo "KUMA_MAINTENANCE_DSN=postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
  echo "KUMA_MIGRATE_DSN=postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
  echo "KUMA_REDIS_URL=${KUMA_REDIS_URL:-redis://127.0.0.1:6379/5}"
  echo "KUMA_AMQP_URL=amqp://kuma_mq:${KUMA_MQ_PASSWORD}@127.0.0.1:5672/%2Fkuma"
  echo "KUMA_MQTT_URL=mqtt://127.0.0.1:1883"
  # MQTT 用户名带 vhost 前缀：插件在 CONNECT 的 host 字段为空时默认落到 vhost '/'，
  # 而本机只有 /kuma → 裸 kuma_mq 会被拒成 134（Bad user name or password）。
  # AMQP 侧（KUMA_AMQP_URL）仍用裸 kuma_mq。2026-10-20 实测：/kuma:kuma_mq 连上。
  echo "KUMA_MQTT_USER=/kuma:kuma_mq"
  echo "KUMA_MQTT_PASSWORD=${KUMA_MQ_PASSWORD}"
  echo "KUMA_JWT_SECRET=${KUMA_JWT_SECRET}"
} > "$OUT"
chmod 600 "$OUT"
echo "WROTE $OUT"
