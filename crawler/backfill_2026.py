"""2026 年历史回填驱动：把 2026-01-01 至今的论文抓入独立库。

用法：venv/bin/python crawler/backfill_2026.py [--db database/backfill_2026.db]
       [--source pubmed|topjournals|biorxiv|all]
- PubMed 关键词通道：esearch 全段一次拉取（上限 2000）。
- 顶刊全量通道：16 刊 ISSN 检索（不经关键词），分批 efetch + 非研究类过滤。
- bioRxiv：按月分段调用 fetch_range（单次分页上限 500 页 × 30 条，避免截断）。
不触碰主库 papers.db。
"""
import argparse
import calendar
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import load_keywords, save_papers  # noqa: E402
from crawler.biorxiv import fetch_range, matches_keywords, parse_record  # noqa: E402
from crawler.pubmed import esearch, efetch, parse_efetch_xml  # noqa: E402

START = date(2026, 1, 1)


def month_ranges(start: date, until: date):
    y, m = start.year, start.month
    cur = date(y, m, 1)
    while cur <= until:
        last = calendar.monthrange(cur.year, cur.month)[1]
        end = min(date(cur.year, cur.month, last), until)
        yield max(cur, start), end
        cur = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)


def backfill_pubmed(db: str, until: date, limit: int = 2000) -> None:
    from crawler.pubmed import build_query
    query = build_query(load_keywords())
    pmids = esearch(query, START, until, limit)
    print(f"[pubmed] esearch 命中 {len(pmids)} 篇（{START} ~ {until}）", flush=True)
    papers = parse_efetch_xml(efetch(pmids))
    n = save_papers(papers, db)
    print(f"[pubmed] 解析 {len(papers)} 篇，新入库 {n} 篇", flush=True)


def _with_retry(fn, attempts: int = 4):
    """网络抖动重试（指数退避），最后一次失败则抛出。"""
    import time as _time
    import requests as _req
    for attempt in range(attempts):
        try:
            return fn()
        except (_req.RequestException, ConnectionError) as e:
            if attempt == attempts - 1:
                raise
            wait = 2 ** attempt
            print(f"  [重试] {type(e).__name__}，{wait}s 后重试（{attempt + 1}/{attempts}）",
                  flush=True)
            _time.sleep(wait)


def backfill_topjournals(db: str, until: date, batch: int = 200) -> None:
    """顶刊全量通道（不经关键词）：按月分段 ISSN 检索（esearch 单次上限约 1 万）
    + 分批 efetch（带重试）+ 非研究类过滤。"""
    import time as _time
    from crawler.pubmed import REQUEST_INTERVAL
    from crawler.top_journals import (build_journal_query, is_research_article,
                                      load_journals)
    query = build_journal_query(load_journals())
    total_new, total_skip = 0, 0
    for since, end in month_ranges(START, until):
        pmids = _with_retry(lambda: esearch(query, since, end, 9999))
        print(f"[topjournals] {since} ~ {end} esearch 命中 {len(pmids)} 篇", flush=True)
        for i in range(0, len(pmids), batch):
            chunk = pmids[i:i + batch]
            papers = _with_retry(lambda: parse_efetch_xml(efetch(chunk)))
            research = [p for p in papers if is_research_article(p)]
            total_new += save_papers(research, db)
            total_skip += len(papers) - len(research)
            _time.sleep(REQUEST_INTERVAL)
        print(f"[topjournals] {since} ~ {end} 完成，累计新入库 {total_new}", flush=True)
    print(f"[topjournals] 合计新入库 {total_new} 篇，剔除非研究类 {total_skip} 篇", flush=True)


def backfill_biorxiv(db: str, until: date) -> None:
    keywords = load_keywords()
    total_new = 0
    for since, end in month_ranges(START, until):
        records = fetch_range(since, end)
        papers = [parse_record(r) for r in records
                  if matches_keywords(r.get("title"), r.get("abstract"), keywords)]
        n = save_papers(papers, db)
        total_new += n
        print(f"[biorxiv] {since} ~ {end} 拉取 {len(records)} 条，命中 {len(papers)}，新入库 {n}",
              flush=True)
    print(f"[biorxiv] 合计新入库 {total_new} 篇", flush=True)


def main():
    ap = argparse.ArgumentParser(description="2026 年历史回填（独立库）")
    ap.add_argument("--db", default="database/backfill_2026.db")
    ap.add_argument("--source", choices=["pubmed", "biorxiv", "topjournals", "all"],
                    default="all")
    ap.add_argument("--start", default=None,
                    help="回填起始日 YYYY-MM-DD（默认 2026-01-01）")
    ap.add_argument("--until", default=None,
                    help="回填截止日 YYYY-MM-DD（默认今天）")
    args = ap.parse_args()
    global START
    if args.start:
        START = date.fromisoformat(args.start)
    until = date.fromisoformat(args.until) if args.until else date.today()
    if args.source in ("pubmed", "all"):
        backfill_pubmed(args.db, until)
    if args.source in ("topjournals", "all"):
        backfill_topjournals(args.db, until)
    if args.source in ("biorxiv", "all"):
        backfill_biorxiv(args.db, until)


if __name__ == "__main__":
    main()
