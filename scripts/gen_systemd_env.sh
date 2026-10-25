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
# MQTT 认证端点的路径密钥（T1）。api 与 rabbitmq.conf 必须用同一个值：
# 8100 被 nginx 全量反代到公网，路径里没有这段就 404，端点不会被人当口令探针枚举。
if ! grep -q '^KUMA_MQ_AUTH_TOKEN=' deploy/.env; then
  TOKEN="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
  echo "KUMA_MQ_AUTH_TOKEN=${TOKEN}" >> deploy/.env
  echo "已生成并写入 deploy/.env 的 KUMA_MQ_AUTH_TOKEN（要同步进 rabbitmq.conf 的四个 URL）"
fi
# 证件号哈希的 pepper（KYC，2026-10-26）。**必须独立于 jwt_secret 且长期不变**：
# 哈希要确定性（§2.3-25"同证件多账号 → 关联标记"靠同证件同哈希），
# 而缺省时 `config.py` 会回落到 jwt_secret —— 轮换 jwt_secret 会让历史关联全部断掉。
# 所以这里缺就生成一份，写死在 .env（Debug.md 是唯一凭据台账，值不进 git）。
if ! grep -q '^KUMA_ID_HASH_PEPPER=' deploy/.env; then
  PEPPER="$(head -c 32 /dev/urandom | base64 | tr -d '/+=')"
  echo "KUMA_ID_HASH_PEPPER=${PEPPER}" >> deploy/.env
  echo "已生成并写入 deploy/.env 的 KUMA_ID_HASH_PEPPER（轮换它等于让历史证件关联失效，别随手换）"
fi
set -a; . deploy/.env; set +a

# kuma 独立 redis（2026-10-22，Task-Server.md §4.1）要求有口令：**缺就报错退出**，
# 绝不退化成"无口令的 6379"——那正是这次要摆脱的配置。
if [ -z "${KUMA_REDIS_PASSWORD:-}" ]; then
  echo "!! deploy/.env 缺 KUMA_REDIS_PASSWORD。生成：head -c 24 /dev/urandom | base64 | tr -d '/+='" >&2
  exit 1
fi

umask 077
OUT=deploy/runtime.env
{
  echo "KUMA_ENV=prod"
  echo "KUMA_DSN=postgresql+asyncpg://kuma_app:${KUMA_APP_PASSWORD}@127.0.0.1:5432/kuma"
  echo "KUMA_MAINTENANCE_DSN=postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
  echo "KUMA_MIGRATE_DSN=postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
  # kuma 自己的 redis 容器：6380 + 口令 + db0。独立实例后不再需要 DB5 做隔离，
  # 但 `kuma:` 前缀保留（它是"哪些 key 属于 kuma"的运维标识，也是 §4.1 的强制 TTL 约定载体）。
  # ⚠️ 口令必须 URL 安全：生成方式已 `tr -d '/+='`，含 @ / : 会破坏这个 DSN。
  echo "KUMA_REDIS_URL=redis://:${KUMA_REDIS_PASSWORD}@127.0.0.1:6380/0"
  echo "KUMA_AMQP_URL=amqp://kuma_mq:${KUMA_MQ_PASSWORD}@127.0.0.1:5672/%2Fkuma"
  echo "KUMA_MQTT_URL=mqtt://127.0.0.1:1883"
  # MQTT 用户名带 vhost 前缀：插件在 CONNECT 的 host 字段为空时默认落到 vhost '/'，
  # 而本机只有 /kuma → 裸 kuma_mq 会被拒成 134（Bad user name or password）。
  # AMQP 侧（KUMA_AMQP_URL）仍用裸 kuma_mq。2026-10-20 实测：/kuma:kuma_mq 连上。
  echo "KUMA_MQTT_USER=/kuma:kuma_mq"
  echo "KUMA_MQTT_PASSWORD=${KUMA_MQ_PASSWORD}"
  echo "KUMA_MQTT_VHOST=/kuma"
  echo "KUMA_MQ_AUTH_TOKEN=${KUMA_MQ_AUTH_TOKEN}"
  echo "KUMA_JWT_SECRET=${KUMA_JWT_SECRET}"
  # 上面已保证存在（缺则生成），所以这里无条件写。
  echo "KUMA_ID_HASH_PEPPER=${KUMA_ID_HASH_PEPPER}"
  # 地图 key（Debug.md 高德节）：此前白名单不透传，key 写进 .env 服务也读不到。
  # 缺行不炸（road 口径 v1 不做，key 可以缺席），有值才写——空串写进去没有意义。
  [ -n "${KUMA_AMAP_KEY:-}" ] && echo "KUMA_AMAP_KEY=${KUMA_AMAP_KEY}"
  [ -n "${KUMA_GOOGLE_KEY:-}" ] && echo "KUMA_GOOGLE_KEY=${KUMA_GOOGLE_KEY}"
} > "$OUT"
chmod 600 "$OUT"
echo "WROTE $OUT"
