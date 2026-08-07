"""Phase 7：PubMed 每日文献抓取（NCBI E-utilities，≤3 请求/秒）。

检索词从 config/keyword_config.yaml 所有组的 items 构造 title/abstract OR 检索式，
esearch（按出版日期段）+ efetch 拉取元数据，只抓 title/abstract/authors/journal/date/doi/url。
解析函数 parse_efetch_xml 可离线单测。
"""
import argparse
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crawler.common import load_keywords, resolve_since, save_papers  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_INTERVAL = 0.4  # 秒，遵守 NCBI ≤3 请求/秒

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def build_query(keywords) -> str:
    """构造 title/abstract OR 检索式。"""
    return "(" + " OR ".join(f'"{kw}"[Title/Abstract]' for kw in keywords) + ")"


def esearch(query: str, since: date, until: date, limit: int) -> list:
    """esearch 返回 PMID 列表（POST 以避免超长 URL）。"""
    resp = requests.post(f"{EUTILS}/esearch.fcgi", data={
        "db": "pubmed", "term": query, "retmax": limit, "retmode": "json",
        "datetype": "pdat", "mindate": since.strftime("%Y/%m/%d"),
        "maxdate": until.strftime("%Y/%m/%d"),
    }, timeout=30)
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def esearch_history(query: str, since: date, until: date) -> tuple:
    """usehistory=y 检索（retmax=0），返回 (WebEnv, query_key, count)。

    PubMed 的 esearch 无论是否 usehistory 都只能取回前 9,999 个 PMID
    （retstart>9998 直接报错），大结果集必须改用 history + efetch 翻页，
    见 efetch_history。
    """
    resp = requests.post(f"{EUTILS}/esearch.fcgi", data={
        "db": "pubmed", "term": query, "retmax": 0, "usehistory": "y",
        "retmode": "json", "datetype": "pdat",
        "mindate": since.strftime("%Y/%m/%d"), "maxdate": until.strftime("%Y/%m/%d"),
    }, timeout=60)
    resp.raise_for_status()
    res = resp.json(strict=False)["esearchresult"]
    return res["webenv"], res["querykey"], int(res["count"])


def efetch_history(webenv: str, qkey: str, retstart: int, retmax: int) -> str:
    """从 history 服务器翻页 efetch（无 9,999 上限），返回 XML。"""
    resp = requests.post(f"{EUTILS}/efetch.fcgi", data={
        "db": "pubmed", "query_key": qkey, "WebEnv": webenv,
        "retstart": retstart, "retmax": retmax, "retmode": "xml",
    }, timeout=120)
    resp.raise_for_status()
    return resp.text


def efetch(pmids) -> str:
    """efetch 拉取 XML 元数据。"""
    resp = requests.post(f"{EUTILS}/efetch.fcgi", data={
        "db": "pubmed", "id": ",".join(pmids), "retmode": "xml",
    }, timeout=60)
    resp.raise_for_status()
    return resp.text


def _pub_date(article) -> str:
    """解析 Journal PubDate 为 YYYY-MM-DD（尽量；缺月/日则截断）。"""
    pd = article.find("./Journal/JournalIssue/PubDate")
    if pd is None:
        return ""
    year = pd.findtext("Year") or ""
    if not year:
        year = (pd.findtext("MedlineDate") or "")[:4]
    month = pd.findtext("Month") or ""
    if month:
        m = MONTHS.get(month[:3].lower(), month)
        month = f"{int(m):02d}" if str(m).isdigit() else str(m)
    day = pd.findtext("Day") or ""
    if day and day.isdigit():
        day = f"{int(day):02d}"
    return "-".join(p for p in (year, month, day) if p)


def parse_efetch_xml(xml_text: str) -> list:
    """离线可测：解析 efetch XML 为论文 dict 列表。"""
    root = ET.fromstring(xml_text)
    papers = []
    for art in root.iter("PubmedArticle"):
        medline = art.find("MedlineCitation")
        article = medline.find("Article")
        pmid = (medline.findtext("PMID") or "").strip()
        title_el = article.find("ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""
        abstract = " ".join("".join(el.itertext()).strip()
                            for el in article.findall("./Abstract/AbstractText")).strip()
        authors = []
        for a in article.findall("./AuthorList/Author"):
            last = a.findtext("LastName") or a.findtext("CollectiveName") or ""
            name = f"{last} {a.findtext('Initials') or ''}".strip()
            if name:
                authors.append(name)
        doi = ""
        for el in article.findall("ELocationID"):
            if el.get("EIdType") == "doi" and el.text:
                doi = el.text.strip()
        if not doi:
            for el in art.findall("./PubmedData/ArticleIdList/ArticleId"):
                if el.get("IdType") == "doi" and el.text:
                    doi = el.text.strip()
        pub_types = [el.text.strip() for el in
                     article.findall("./PublicationTypeList/PublicationType") if el.text]
        papers.append({
            "paper_id": f"pubmed:{doi or pmid}", "title": title, "abstract": abstract,
            "authors": ", ".join(authors),
            "journal": article.findtext("./Journal/Title") or "",
            "date": _pub_date(article), "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "source": "pubmed",
            "pub_types": pub_types,
        })
    return papers


def main():
    ap = argparse.ArgumentParser(description="PubMed 每日文献抓取")
    ap.add_argument("--since", default="yesterday", help="yesterday 或 YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=100, help="最多抓取条数（默认 100）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    since = resolve_since(args.since)
    until = date.today()
    query = build_query(load_keywords())
    pmids = esearch(query, since, until, args.limit)
    print(f"esearch 命中 {len(pmids)} 篇（{since} ~ {until}）")
    if not pmids:
        return
    time.sleep(REQUEST_INTERVAL)
    papers = parse_efetch_xml(efetch(pmids))
    n = save_papers(papers, args.db)
    print(f"解析 {len(papers)} 篇，新入库 {n} 篇（重复自动忽略）")


if __name__ == "__main__":
    main()
