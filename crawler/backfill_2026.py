"""历史回填驱动：把 START 至今（或 --until）的论文抓入独立库。

用法：venv/bin/python crawler/backfill_2026.py [--db database/backfill_2026.db]
       [--source pubmed|topjournals|biorxiv|all] [--start YYYY-MM-DD] [--until YYYY-MM-DD]
- PubMed 关键词通道：按月分段 + usehistory 检索 + efetch 翻页（PubMed esearch
  最多取回 9,999 个 PMID，单月命中可超 2 万），分批解析入库（带重试）。
- 顶刊全量通道：16 刊 ISSN 检索（不经关键词），分批 efetch + 非研究类过滤。
- bioRxiv：按月分段调用 fetch_range（单次分页上限 500 页 × 30 条，避免截断）。
不触碰主库 papers.db。
"""
import argparse
import calendar
import sys
from datetime import date, timedelta
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


# PubMed 检索后端硬上限：无论 esearch 还是 history+efetch，单次检索最多取回
# 前 9,999 条（retstart>9998 直接报错）。因此 >CAP 的日期段必须继续细分：
# 先二分日期；若单日仍超上限（每年 1 月 1 日堆积了仅标年份的记录，可达 1.8 万），
# 再把关键词 OR 式二分拆分分别检索（重叠部分由 save_papers 去重）。
PMID_CAP = 9500  # 留安全边距
_ONE_DAY = timedelta(days=1)


def backfill_pubmed(db: str, until: date, batch: int = 200) -> None:
    """PubMed 关键词通道：按月分段 → 段内自适应细分（日期二分 → 关键词二分）
    → history+efetch 翻页取全量，分批解析入库（带重试）。"""
    import time as _time
    from crawler.pubmed import (REQUEST_INTERVAL, build_query,
                                esearch_history, efetch_history)
    keywords = load_keywords()
    total_new = 0
    for since, end in month_ranges(START, until):
        n = _crawl_pubmed_range(keywords, since, end, db, batch,
                                build_query, esearch_history, efetch_history,
                                REQUEST_INTERVAL, _time)
        total_new += n
        print(f"[pubmed] {since} ~ {end} 完成，累计新入库 {total_new}", flush=True)
    print(f"[pubmed] 合计新入库 {total_new} 篇", flush=True)


def _crawl_pubmed_range(terms, since: date, end: date, db: str, batch: int,
                        build_query, esearch_history, efetch_history,
                        interval, _time, depth: int = 0) -> int:
    """自适应细分检索一个日期段，返回新入库篇数。"""
    query = build_query(terms)
    webenv, qkey, count = _with_retry(lambda: esearch_history(query, since, end))
    print(f"[pubmed]   {since} ~ {end}（{len(terms)} 词）命中 {count}", flush=True)
    if count == 0:
        return 0
    if count > PMID_CAP:
        if since < end:  # 先二分日期
            mid = since + (end - since) // 2
            left = _crawl_pubmed_range(terms, since, mid, db, batch, build_query,
                                       esearch_history, efetch_history, interval,
                                       _time, depth + 1)
            right = _crawl_pubmed_range(terms, mid + _ONE_DAY, end, db, batch,
                                        build_query, esearch_history, efetch_history,
                                        interval, _time, depth + 1)
            return left + right
        if len(terms) > 1:  # 单日仍超上限：二分关键词
            half = len(terms) // 2
            left = _crawl_pubmed_range(terms[:half], since, end, db, batch,
                                       build_query, esearch_history, efetch_history,
                                       interval, _time, depth + 1)
            right = _crawl_pubmed_range(terms[half:], since, end, db, batch,
                                        build_query, esearch_history, efetch_history,
                                        interval, _time, depth + 1)
            return left + right
        print(f"[pubmed] [warn] 单词单日命中 {count} 超上限，只能取前 {PMID_CAP} 条",
              flush=True)
    total_new = 0
    for retstart in range(0, min(count, 9999), batch):
        try:
            papers = _with_retry(lambda: parse_efetch_xml(
                efetch_history(webenv, qkey, retstart, batch)))
        except Exception as e:  # 单页持续失败不拖垮整夜回填；重跑本脚本可补齐
            print(f"[pubmed] [warn] {since} ~ {end} 第 {retstart} 页放弃：{e}", flush=True)
            continue
        total_new += save_papers(papers, db)
        _time.sleep(interval)
    return total_new


def _with_retry(fn, attempts: int = 4):
    """网络抖动/响应截断重试（指数退避），最后一次失败则抛出。

    ET.ParseError：efetch 大响应偶发截断产生 malformed XML，属可重试故障。"""
    import time as _time
    import xml.etree.ElementTree as _ET
    import requests as _req
    retryable = (_req.RequestException, ConnectionError, _ET.ParseError)
    for attempt in range(attempts):
        try:
            return fn()
        except retryable as e:
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
