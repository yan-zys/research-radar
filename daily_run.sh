#!/usr/bin/env bash
# Research Radar 每日流水线：
#   抓取(pubmed/biorxiv/arxiv) → 关键词过滤 → AI 评分 → 排序
#   → 生成并发送邮件日报 → 反馈学习（降权/挖候选词）
# 单步失败不中断后续步骤，全部记录到 logs/daily_YYYY-MM-DD.log
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
source venv/bin/activate

mkdir -p logs
LOG="logs/daily_$(date +%F).log"

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

run_step "crawler: pubmed"        python crawler/pubmed.py
run_step "crawler: biorxiv"       python crawler/biorxiv.py
run_step "crawler: arxiv"         python crawler/arxiv.py
run_step "filter: keyword_filter" python processing/keyword_filter.py
run_step "analyze: paper_analyzer" python processing/paper_analyzer.py
run_step "ranking: scoring"       python ranking/scoring.py
run_step "email: generate+send"   python email/generate_email.py --send
run_step "feedback: learning"     python feedback/learning.py

echo "[$(date '+%T')] 全部步骤结束，日志：$LOG"
