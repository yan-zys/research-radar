#!/bin/bash
# Phase 11：每日全流程调度（抓取 → 过滤 → AI 评分 → 打分 → 生成并发送日报）
set -e
set -o pipefail
cd ~/research_radar
source venv/bin/activate
mkdir -p logs
LOG="logs/$(date +%F).log"
{
python crawler/pubmed.py --since yesterday
python crawler/biorxiv.py --since yesterday
python crawler/arxiv.py --since yesterday
python processing/keyword_filter.py
python processing/paper_analyzer.py
python ranking/scoring.py
python email/generate_email.py --send
} 2>&1 | tee "$LOG"
