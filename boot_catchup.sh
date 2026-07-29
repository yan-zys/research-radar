#!/usr/bin/env bash
# 开机补发：WSL 启动后检查当天日报是否已成功发送，未发送则自动补跑 daily_run.sh。
# 触发方式：cron @reboot（WSL 启动）+ Windows 登录计划任务（确保 WSL 被拉起）。
# 与 08:30 定时任务通过 daily_run.sh 内的 flock 互斥；节假日由 daily_run.sh 自身跳过。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sleep 60  # 等待网络与系统就绪

TODAY="$(date +%F)"
LOG="$ROOT/logs/daily_$TODAY.log"

if [ -f "$LOG" ] && grep -q "邮件已发送至" "$LOG"; then
  echo "[$(date '+%F %T')] 当天日报已发送，无需补发"
  exit 0
fi

echo "[$(date '+%F %T')] 当天日报未发送，自动补跑 daily_run.sh"
"$ROOT/daily_run.sh"
