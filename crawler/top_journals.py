"""顶刊通道：不经过关键词检索，按期刊清单直接抓取最新论文进主池。

期刊清单在 config/top_journals.yaml（NLM 期刊名 + ISSN），esearch 用 ISSN 检索
（避免期刊名歧义），efetch/解析/入库复用 crawler.pubmed 与 crawler.common。
抓回的论文由 keyword_filter 正常建 paper_scores（命中关键词则 rule_score>0），
再由 paper_analyzer 对顶刊论文补 AI 语义评分，最终 scoring 统一四维排序。
"""
import argparse
import re
import sys
import time
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import resolve_since, save_papers  # noqa: E402
from crawler.pubmed import REQUEST_INTERVAL, efetch, esearch, parse_efetch_xml  # noqa: E402

# 非研究论文的 PubMed PublicationType（新闻/社论/来信/更正/撤稿等，不入库）
EXCLUDE_PUB_TYPES = {
    "news", "editorial", "comment", "letter", "biography", "interview",
    "historical article", "published erratum", "retracted publication",
    "retraction of publication", "corrected and republished article",
    "newspaper article", "lecture", "portrait", "obituary", "patient education handout",
}
# 标题兜底（PublicationType 缺失时）：更正/撤稿/每日新闻栏目
EXCLUDE_TITLE_RE = re.compile(
    r"^\s*(author correction|publisher correction|correction|corrigendum|addendum|"
    r"retraction|editorial|editor's note|daily briefing|briefing chat|news feature|"
    r"news & views|where i work)\b", re.I)
# Nature 新闻版块 DOI 前缀（d41586 = Nature 新闻团队）
EXCLUDE_DOI_PREFIXES = ("10.1038/d41586",)


def is_research_article(p) -> bool:
    """仅保留研究/综述类论文：按 PublicationType、DOI 前缀、标题三重过滤。"""
    types = {t.lower() for t in (p.get("pub_types") or [])}
    if types & EXCLUDE_PUB_TYPES:
        return False
    doi = (p.get("doi") or "").lower()
    if any(doi.startswith(pre) for pre in EXCLUDE_DOI_PREFIXES):
        return False
    if EXCLUDE_TITLE_RE.match(p.get("title") or ""):
        return False
    return True


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
    research = [p for p in papers if is_research_article(p)]
    n = save_papers(research, args.db)
    print(f"解析 {len(papers)} 篇，剔除非研究类 {len(papers) - len(research)} 篇，"
          f"新入库 {n} 篇（重复自动忽略）")


if __name__ == "__main__":
    main()
