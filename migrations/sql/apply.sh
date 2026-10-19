#!/usr/bin/env bash
# 按序执行 migrations/sql/*.sql（在爱商汇上跑）
# 口令来自 deploy/.env，经 psql 变量传入，不写进任何 SQL 文件
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ../../deploy/.env; set +a

for f in $(ls [0-9]*.sql | sort); do
  echo "=== APPLY $f"
  docker exec -i \
    -e APPPW="$KUMA_APP_PASSWORD" -e ROPW="$KUMA_RO_PASSWORD" \
    kuma-postgres psql -U kuma_owner -d kuma \
    -v ON_ERROR_STOP=1 -v apppw="$KUMA_APP_PASSWORD" -v ropw="$KUMA_RO_PASSWORD" \
    -f - < "$f"
done
echo "=== DONE"
