"""顶刊通道：不经过关键词检索，按期刊清单直接抓取最新论文进主池。

期刊清单在 config/top_journals.yaml（NLM 期刊名 + ISSN），esearch 用 ISSN 检索
（避免期刊名歧义），efetch/解析/入库复用 crawler.pubmed 与 crawler.common。
抓回的论文由 keyword_filter 正常建 paper_scores（命中关键词则 rule_score>0），
再由 paper_analyzer 对顶刊论文补 AI 语义评分，最终 scoring 统一四维排序。
"""
import argparse
import sys
import time
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import resolve_since, save_papers  # noqa: E402
from crawler.pubmed import REQUEST_INTERVAL, efetch, esearch, parse_efetch_xml  # noqa: E402


def load_journals(path=None) -> list:
    """读取期刊清单 [{name, issn}]。"""
    p = Path(path) if path else ROOT / "config" / "top_journals.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("journals") or []


def build_journal_query(journals) -> str:
    """构造 ISSN OR 检索式。"""
    return "(" + " OR ".join(f'"{j["issn"]}"[issn]' for j in journals) + ")"


def journal_names(path=None) -> list:
    """NLM 期刊名小写列表（供 paper_analyzer 匹配 papers.journal）。"""
    return [str(j["name"]).lower() for j in load_journals(path)]


def main():
    ap = argparse.ArgumentParser(description="顶刊每日抓取（不经关键词）")
    ap.add_argument("--since", default="yesterday", help="yesterday 或 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=200, help="最多抓取条数（默认 200）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    since = resolve_since(args.since)
    until = date.today()
    query = build_journal_query(load_journals())
    pmids = esearch(query, since, until, args.limit)
    print(f"顶刊 esearch 命中 {len(pmids)} 篇（{since} ~ {until}）")
    if not pmids:
        return
    time.sleep(REQUEST_INTERVAL)
    papers = parse_efetch_xml(efetch(pmids))
    n = save_papers(papers, args.db)
    print(f"解析 {len(papers)} 篇，新入库 {n} 篇（重复自动忽略）")


if __name__ == "__main__":
    main()
