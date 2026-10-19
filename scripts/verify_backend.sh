#!/usr/bin/env bash
# 后端落地验证（爱商汇）：导入 → 单测 → 真实库/AMQP 集成测试 → alembic baseline → 起 api 打 healthz
set -uo pipefail
cd /data/kuma_automobile_server
PY=/data/tiger/miniconda3/envs/kuma/bin/python

set -a; . deploy/.env; set +a
export KUMA_ENV=dev
export KUMA_DSN="postgresql+asyncpg://kuma_app:${KUMA_APP_PASSWORD}@127.0.0.1:5432/kuma"
export KUMA_MIGRATE_DSN="postgresql+asyncpg://kuma_owner:${KUMA_PG_OWNER_PASSWORD}@127.0.0.1:5432/kuma"
export KUMA_MAINTENANCE_DSN="$KUMA_MIGRATE_DSN"
export KUMA_AMQP_URL="amqp://kuma_mq:${KUMA_MQ_PASSWORD}@127.0.0.1:5672/%2Fkuma"
export KUMA_MQTT_URL="mqtt://127.0.0.1:1883"
export KUMA_MQTT_USER=kuma_mq
export KUMA_MQTT_PASSWORD="${KUMA_MQ_PASSWORD}"
export KUMA_JWT_SECRET="$(head -c 24 /dev/urandom | base64)"

echo "=== 1) 导入与模型映射"
$PY -c "from src.kuma.api import create_app; from src.kuma.models import Base; print('IMPORT_OK mapped_tables=' + str(len(Base.metadata.tables)))"

echo "=== 2) 单元测试（不连库）"
$PY -m pytest -m "not db and not mq" --tb=short 2>&1 | tail -12

echo "=== 3) 集成测试（真实 PG + RabbitMQ）"
$PY -m pytest -m db --tb=short 2>&1 | tail -25

echo "=== 4) Alembic（baseline 已 stamp，这里应用 0002）"
/data/tiger/miniconda3/envs/kuma/bin/alembic upgrade head 2>&1 | tail -4
/data/tiger/miniconda3/envs/kuma/bin/alembic current 2>&1 | tail -2

echo "=== 5) 起 api 进程打 healthz"
nohup /data/tiger/miniconda3/envs/kuma/bin/uvicorn main:app --host 127.0.0.1 --port 8100 --workers 1 > /tmp/kuma_api.log 2>&1 &
API_PID=$!
sleep 6
curl -s http://127.0.0.1:8100/healthz; echo
echo "--- 注册 + 登录 + 越权 403 冒烟"
U="smoke_$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
curl -s -X POST http://127.0.0.1:8100/api/v1/auth/register -H 'content-type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"Sup3r-Secret!\",\"region_id\":\"TH\"}" ; echo
TOKEN=$(curl -s -X POST http://127.0.0.1:8100/api/v1/auth/login -H 'content-type: application/json' \
  -d "{\"username\":\"$U\",\"password\":\"Sup3r-Secret!\"}" | $PY -c "import sys,json;print(json.load(sys.stdin).get('access_token',''))")
echo "token_len=${#TOKEN}"
curl -s http://127.0.0.1:8100/api/v1/auth/me -H "authorization: Bearer $TOKEN"; echo
curl -s -o /dev/null -w 'vehicles_as_consumer=%{http_code}\n' http://127.0.0.1:8100/api/v1/vehicles -H "authorization: Bearer $TOKEN"
echo "--- 公开端点冒烟（portal 用，不带 token）"
curl -s -o /dev/null -w 'public_vehicle_types=%{http_code}\n' 'http://127.0.0.1:8100/api/v1/public/vehicle-types?region_id=TH&limit=3'
curl -s -o /dev/null -w 'public_dealers=%{http_code}\n' 'http://127.0.0.1:8100/api/v1/public/dealers?region_id=TH'
curl -s -o /dev/null -w 'public_type_detail_404=%{http_code}\n' http://127.0.0.1:8100/api/v1/public/vehicle-types/__no_such__
kill $API_PID 2>/dev/null
echo "=== 6) api 进程内存"
grep -e "Max RSS" /proc/$API_PID/status 2>/dev/null || echo "(进程已退出)"
tail -5 /tmp/kuma_api.log
