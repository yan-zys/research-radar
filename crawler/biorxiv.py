"""Phase 7：bioRxiv 每日文献抓取。

api.biorxiv.org/details/biorxiv/<from>/<to> 按日期段拉取（游标分页），
本地用关键词对 title+abstract 粗筛。解析函数可离线单测。
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import load_keywords, resolve_since, save_papers  # noqa: E402

API = "https://api.biorxiv.org/details/biorxiv"
REQUEST_INTERVAL = 0.4  # 秒
MAX_PAGES = 500  # 游标分页上限（API 每页 30 条，月级回填约需 200+ 页）


def parse_record(rec: dict) -> dict:
    """离线可测：单条 API 记录转为论文 dict。"""
    doi = (rec.get("doi") or "").strip()
    return {
        "paper_id": f"biorxiv:{doi}",
        "title": (rec.get("title") or "").strip(),
        "abstract": (rec.get("abstract") or "").strip(),
        "authors": (rec.get("authors") or "").strip(),
        "journal": "bioRxiv",
        "date": (rec.get("date") or "").strip(),
        "doi": doi,
        "url": f"https://www.biorxiv.org/content/{doi}",
        "source": "biorxiv",
    }


def matches_keywords(title: str, abstract: str, keywords) -> bool:
    """title+abstract 大小写不敏感子串粗筛。"""
    text = f"{title or ''} {abstract or ''}".lower()
    return any(kw.lower() in text for kw in keywords)


def fetch_range(since: date, until: date) -> list:
    """按日期段游标分页拉取全部记录（以 messages.total 为终止依据，
    不能按"不足一页"判断——该 API 每页固定 30 条）。"""
    records = []
    cursor = 0
    total = None
    for _ in range(MAX_PAGES):
        url = f"{API}/{since.isoformat()}/{until.isoformat()}/{cursor}"
        data = None
        for attempt in range(4):  # 网络抖动重试，指数退避
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        collection = data.get("collection") or []
        if not collection:
            break
        if total is None:
            messages = data.get("messages") or []
            try:
                total = int(messages[0].get("total")) if messages else None
            except (TypeError, ValueError):
                total = None
        records.extend(collection)
        cursor += len(collection)
        if total is not None and cursor >= total:
            break
        time.sleep(REQUEST_INTERVAL)
    return records


def main():
    ap = argparse.ArgumentParser(description="bioRxiv 每日文献抓取")
    ap.add_argument("--since", default="yesterday", help="yesterday 或 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=100, help="最多入库条数（默认 100）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    since = resolve_since(args.since)
    until = date.today()
    keywords = load_keywords()
    records = fetch_range(since, until)
    print(f"bioRxiv {since} ~ {until} 共拉取 {len(records)} 条")
    papers = [parse_record(r) for r in records
              if matches_keywords(r.get("title"), r.get("abstract"), keywords)][:args.limit]
    n = save_papers(papers, args.db)
    print(f"关键词粗筛通过 {len(papers)} 篇，新入库 {n} 篇（重复自动忽略）")


if __name__ == "__main__":
    main()
