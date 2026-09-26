#!/bin/sh
# ELF2 中继主控「定时重启」专用 helper
#
# 为什么要有这个脚本：Web 应用（app.py）跑在 elf 用户下，重启板卡需要 root。
# 与其给 elf 通用 sudo，不如只把这一个脚本放进 sudoers 白名单——应用能重启，
# 但拿不到别的特权。
#
# 部署（需 root）：
#   install -m 755 -o root -g root elf2-reboot.sh /usr/local/sbin/elf2-reboot.sh
#   install -m 440 -o root -g root 99-elf2-reboot.sudoers /etc/sudoers.d/99-elf2-reboot
#   visudo -c          # 语法自检
#
# 自检（不重启）：
#   sudo -n /usr/local/sbin/elf2-reboot.sh --check
set -e

if [ "$1" = "--check" ]; then
  echo "elf2-reboot: sudoers 可用（当前 $(id -un)）"
  exit 0
fi

# 先把数据刷盘，再做一次 PTT 兜底释放（GPIO3_A1 = 全局 97），避免带键重启
sync
if [ -d /sys/class/gpio/gpio97 ]; then
  echo 0 > /sys/class/gpio/gpio97/value 2>/dev/null || true
fi

logger -t elf2-reboot "定时/手动重启：${1:-manual}"
if command -v systemctl >/dev/null 2>&1; then
  exec systemctl reboot
fi
exec /sbin/reboot
