#!/usr/bin/env bash
# kuma_automobile_server / deploy/setup_swap.sh
# 爱商汇服务器（38.47.109.16）加 2G swapfile + vm.swappiness=10
# 目的：服务器 SwapTotal=0，内存触顶会被 OOM killer 直接杀进程（最可能杀掉 RSS 最大的
#       orio-search-api）。加 swap 把"被杀"变成"变慢"。
# 用法：sudo bash setup_swap.sh
set -euo pipefail

echo "== BEFORE =="
free -m
swapon --show || true
df -h / | tail -1

if ! findmnt -no FSTYPE -T / >/dev/null 2>&1; then
  echo "warn: cannot detect root fs" >&2
fi

# 1) 创建 swapfile（已存在则复用）
if [ ! -f /swapfile ]; then
  echo "== CREATE swapfile =="
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
else
  echo "swapfile already exists, size: $(stat -c %s /swapfile) bytes"
fi
chmod 600 /swapfile

# 2) 初始化为 swap（已是 swap 则跳过）
if [ "$(blkid -o value -s TYPE /swapfile 2>/dev/null || true)" != "swap" ]; then
  mkswap /swapfile
else
  echo "already has swap signature"
fi

# 3) 启用（已启用则跳过）
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -qx /swapfile; then
  swapon /swapfile
else
  echo "already active"
fi

# 4) 开机持久化
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 5) swappiness=10（持久化到独立文件，不改 /etc/sysctl.conf）
printf 'vm.swappiness=10\n' > /etc/sysctl.d/99-kuma.conf
sysctl -p /etc/sysctl.d/99-kuma.conf

echo "== AFTER =="
free -m
swapon --show
sysctl vm.swappiness
grep '/swapfile' /etc/fstab
df -h / | tail -1
