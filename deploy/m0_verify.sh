#!/usr/bin/env bash
# M0 验收脚本（在爱商汇上执行：bash /data/kuma_automobile_server/deploy/m0_verify.sh）
set -u
echo "=== 容器状态 ==="
docker ps --filter name=kuma- --format '{{.Names}} | {{.Status}} | {{.Ports}}'

echo "=== 监听（必须只有 127.0.0.1）==="
ss -lnt | grep -E ':(5432|5672|15672|1883)\b' || echo "NO_LISTEN (异常)"

echo "=== PostgreSQL ==="
docker exec kuma-postgres psql -U kuma_owner -d kuma -tAc 'select extname from pg_extension'
docker exec kuma-postgres psql -U kuma_owner -d kuma -tAc 'select default_version from pg_available_extensions where name=$$postgis$$'
docker exec kuma-postgres psql -U kuma_owner -d kuma -tAc 'show timezone'
docker exec kuma-postgres psql -U kuma_owner -d kuma -tAc 'show shared_buffers'
docker exec kuma-postgres psql -U kuma_owner -d kuma -tAc 'show max_connections'

echo "=== RabbitMQ 插件 / vhost / 用户 ==="
docker exec kuma-rabbitmq rabbitmq-plugins list --enabled --quiet
docker exec kuma-rabbitmq rabbitmqctl list_vhosts
docker exec kuma-rabbitmq rabbitmqctl list_users
docker exec kuma-rabbitmq rabbitmqctl list_permissions -p /kuma

echo "=== RabbitMQ 内存水位与监听 ==="
docker exec kuma-rabbitmq rabbitmq-diagnostics status 2>/dev/null | grep -iE 'vm_memory_limit|listeners_tcp|mqtt' | head -8

echo "=== 内存 ==="
docker stats --no-stream --format '{{.Name}} | {{.MemUsage}} | {{.MemPerc}}' kuma-postgres kuma-rabbitmq
free -m | head -2
