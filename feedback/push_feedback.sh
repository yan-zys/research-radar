#!/usr/bin/env bash
# 反馈数据同步：feedback server 在本机运行、点击写入 input/user_feedback/*.jsonl，
# 本脚本把新增反馈推送到 GitHub，供 Actions 上的反馈学习步骤使用。
# 建议 cron：*/30 * * * * /home/zhangyanshu/research_radar/feedback/push_feedback.sh
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

git pull --rebase -q origin main || exit 0
git add input/user_feedback/
if git diff --cached --quiet; then
  exit 0
fi
git commit -q -m "feedback: 同步本机反馈 $(date '+%F %T')"
git push -q origin main
