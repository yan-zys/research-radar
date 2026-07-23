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
MAX_PAGES = 50  # 游标分页上限，防止异常分页死循环


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
    """按日期段游标分页拉取全部记录。"""
    records = []
    cursor = 0
    for _ in range(MAX_PAGES):
        url = f"{API}/{since.isoformat()}/{until.isoformat()}/{cursor}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        collection = resp.json().get("collection") or []
        if not collection:
            break
        records.extend(collection)
        cursor += len(collection)
        if len(collection) < 100:
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
