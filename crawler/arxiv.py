"""Phase 7：arXiv 每日文献抓取（export.arxiv.org API，q-bio 分类 + 关键词）。

检索词从 config/keyword_config.yaml 所有组的 items 构造，
本地按 published 日期过滤。解析函数 parse_atom 可离线单测。
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import load_keywords, resolve_since, save_papers  # noqa: E402

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def build_query(keywords) -> str:
    """q-bio 分类 + 关键词 OR 检索式。"""
    kw = " OR ".join(f'all:"{k}"' for k in keywords)
    return f"cat:q-bio.* AND ({kw})"


def fetch(query: str, limit: int) -> str:
    """按提交日期倒序拉取 Atom XML。"""
    resp = requests.get(API, params={
        "search_query": query, "start": 0, "max_results": limit,
        "sortBy": "submittedDate", "sortOrder": "descending",
    }, timeout=60)
    resp.raise_for_status()
    return resp.text


def parse_atom(xml_text: str, since: date) -> list:
    """离线可测：解析 Atom XML 为论文 dict 列表（过滤早于 since 的条目）。"""
    root = ET.fromstring(xml_text)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        published = entry.findtext(f"{ATOM}published") or ""
        day = published[:10]
        if day and day < since.isoformat():
            continue
        url = (entry.findtext(f"{ATOM}id") or "").strip()
        arxiv_id = url.rsplit("/", 1)[-1]
        doi_el = entry.find(f"{ARXIV}doi")
        doi = doi_el.text.strip() if doi_el is not None and doi_el.text else ""
        papers.append({
            "paper_id": f"arxiv:{doi or arxiv_id}",
            "title": " ".join((entry.findtext(f"{ATOM}title") or "").split()),
            "abstract": " ".join((entry.findtext(f"{ATOM}summary") or "").split()),
            "authors": ", ".join(a.findtext(f"{ATOM}name") or ""
                                 for a in entry.findall(f"{ATOM}author")),
            "journal": "arXiv",
            "date": day,
            "doi": doi,
            "url": url,
            "source": "arxiv",
        })
    return papers


def main():
    ap = argparse.ArgumentParser(description="arXiv 每日文献抓取")
    ap.add_argument("--since", default="yesterday", help="yesterday 或 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=50, help="最多抓取条数（默认 50）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    since = resolve_since(args.since)
    query = build_query(load_keywords())
    xml_text = fetch(query, args.limit)
    papers = parse_atom(xml_text, since)
    n = save_papers(papers, args.db)
    print(f"arXiv 返回条目过滤后 {len(papers)} 篇（since {since}），新入库 {n} 篇（重复自动忽略）")


if __name__ == "__main__":
    main()
