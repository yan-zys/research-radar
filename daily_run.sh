#!/usr/bin/env bash
# Research Radar 每日流水线（节假日感知，决策见 scheduler/holiday.py）：
#   法定节假日整天跳过；节后首个非节假日爬虫 --since 取节前最后非节假日，
#   自动合并节假日期间文献为一封日报。
#   抓取(pubmed/biorxiv/arxiv/top_journals) → 关键词过滤 → AI 评分 → 排序
#   → 生成并发送邮件日报 →（周五，逢节假日提前）追加发送周报（--days 7）
#   →（每月最后一天，逢节假日提前）追加发送月报（--days 30）→ 反馈学习
# 单步失败不中断后续步骤，全部记录到 logs/daily_YYYY-MM-DD.log
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source venv/bin/activate

# 防并发：同一时刻只允许一个流水线实例（08:30 cron 与开机补发可能撞车）
exec 9>"$ROOT/.daily_run.lock"
if ! flock -n 9; then
  echo "[$(date '+%T')] 已有 daily_run 实例在运行，本次退出"
  exit 0
fi

mkdir -p logs
LOG="logs/daily_$(date +%F).log"

# 节假日调度决策：SKIP=1 或 DAILY_SINCE=YYYY-MM-DD / WEEKLY=0|1 / MONTHLY=0|1
DECISION="$(python scheduler/holiday.py)"
eval "$DECISION"
if [ "${SKIP:-0}" = "1" ]; then
  echo "[$(date '+%T')] 今天是法定节假日，跳过全部发送" | tee -a "$LOG"
  exit 0
fi
SINCE="${DAILY_SINCE:-yesterday}"

step() {
  echo "" >>"$LOG"
  echo "===== $(date '+%F %T') :: $1 =====" >>"$LOG"
  echo "[$(date '+%T')] $1"
}

run_step() {
  local name="$1"; shift
  step "$name"
  if "$@" >>"$LOG" 2>&1; then
    tail -n 1 "$LOG"
  else
    echo "[warn] $name 失败（详见 $LOG）" | tee -a "$LOG"
  fi
}

if [ "$SINCE" != "yesterday" ]; then
  step "节假日后合并日报：爬虫 --since $SINCE（覆盖节假日空窗）"
fi
run_step "crawler: pubmed"        python crawler/pubmed.py --since "$SINCE"
run_step "crawler: biorxiv"       python crawler/biorxiv.py --since "$SINCE"
run_step "crawler: arxiv"         python crawler/arxiv.py --since "$SINCE"
run_step "crawler: top_journals"  python crawler/top_journals.py --since "$SINCE"
run_step "filter: keyword_filter" python processing/keyword_filter.py
run_step "analyze: paper_analyzer" python processing/paper_analyzer.py
run_step "ranking: scoring"       python ranking/scoring.py
run_step "email: daily generate+send" python email/generate_email.py --send
if [ "${WEEKLY:-0}" = "1" ]; then
  run_step "email: weekly digest" python email/generate_digest.py --days 7 --send
fi
if [ "${MONTHLY:-0}" = "1" ]; then
  run_step "email: monthly digest" python email/generate_digest.py --days 30 --send
fi
run_step "feedback: learning"     python feedback/learning.py

echo "[$(date '+%T')] 全部步骤结束，日志：$LOG"
