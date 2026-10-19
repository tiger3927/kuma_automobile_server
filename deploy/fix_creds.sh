#!/usr/bin/env bash
# 把 deploy/.env 里的口令同步到容器内实际账号（首次建容器后、或轮换口令后运行）
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a

docker exec kuma-postgres psql -U kuma_owner -d kuma -c "ALTER USER kuma_owner WITH PASSWORD '${KUMA_PG_OWNER_PASSWORD}';"
docker exec kuma-rabbitmq rabbitmqctl change_password kuma_mq "${KUMA_MQ_PASSWORD}"

echo "--- 验证 TCP 口令登录 ---"
docker exec -e PGPASSWORD="${KUMA_PG_OWNER_PASSWORD}" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_owner -d kuma -tAc "select 'PG_TCP_LOGIN_OK'"
docker exec kuma-rabbitmq rabbitmqctl authenticate_user kuma_mq "${KUMA_MQ_PASSWORD}" \
  && echo "MQ_AUTH_OK"
echo CREDS_SYNCED
