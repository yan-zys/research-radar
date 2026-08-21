"""次日生效：消费日报网页提交的关键词/文献请求（input/keyword_requests/*.jsonl）。

由 daily_run.sh 在文献抓取之前运行，使当天检索立即使用新词：

- type=keywords（用户手写方向词）：按逗号/分号/换行拆分、去重、按科研画像归类分组
  （归不到 species/methods 的进 concept；组不存在时回退 concept），以
  source=user_page, weight=2, locked=true 直接入库——用户直接输入等同种子词，
  权重最高且不随负反馈降权；
- type=papers（DOI/PMID，每文件合计 ≤10 篇）：逐条解析 title+abstract
  （PMID→efetch；DOI→PubMed esearch[doi] 转 PMID→efetch，失败回退 Crossref），
  汇总后一次 AI 调用提炼 ≤10 个检索词，以 source=paper_page, weight=1,
  locked=false 入库——文献扩展词权重低，可随负反馈降权；
- 与现有配置词条、archive_rejected.yaml 去重（normalize 同 keyword_engine）；
- 已处理文件名记录到 keyword_engine/processed_page_requests.log；无文件时正常退出。
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from crawler.pubmed import EUTILS, efetch, parse_efetch_xml  # noqa: E402
from feedback.learning import dump_config_with_header  # noqa: E402
from keyword_engine.expand_keywords import normalize  # noqa: E402
from keyword_engine.merge_to_config import classify  # noqa: E402

SPLIT_RE = re.compile(r"[,，、;；\n]+")
MAX_PAPERS = 10
MAX_NEW_FROM_PAPERS = 10
CROSSREF = "https://api.crossref.org/works"

PAPER_MINING_PROMPT = """你是科研文献检索助手。下面是用户标记为"感兴趣"的 {n} 篇论文的标题与摘要。
请提炼出最适合用于文献检索（PubMed title/abstract 匹配）的英文关键词或短语，至多 {max_new} 个。
要求：
- 覆盖这些论文共同的核心研究方向（物种/系统、方法技术、科学概念）；
- 每个词 1-4 个单词，可检索、不过宽（避免 biology、 research 之类泛词）；
- 不要与已有词库重复：\n{existing}
严格输出 JSON：{{"keywords": ["...", "..."]}}

【论文列表】
{papers}
"""


def load_processed(log_path: Path) -> set:
    if not log_path.exists():
        return set()
    return {l.strip() for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()}


def mark_processed(log_path: Path, names) -> None:
    if not names:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for name in names:
            f.write(f"{name}\n")


def read_requests(path: Path) -> list:
    """读取一个请求 jsonl，跳过坏行。"""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[跳过] {path.name} 中的坏行: {line[:80]}")
    return entries


def split_keywords(text: str) -> list:
    """按中英文逗号/顿号/分号/换行拆分，去空白去重（保序）。"""
    seen, out = set(), []
    for piece in SPLIT_RE.split(text or ""):
        kw = piece.strip()
        if not kw:
            continue
        norm = normalize(kw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(kw)
    return out


def pick_group(keyword: str, profile: dict, config: dict) -> str:
    """按画像归类分组；归类结果不在配置组里时回退 concept。"""
    group = classify(keyword, profile)
    if group not in (config.get("keywords") or {}):
        group = "concept"
    return group


def load_rejected_norms(archive_path: Path) -> set:
    if not archive_path.exists():
        return set()
    data = yaml.safe_load(archive_path.read_text(encoding="utf-8")) or []
    return {normalize(c.get("keyword", "")) for c in data}


def existing_norms(config: dict) -> set:
    return {normalize(i["keyword"])
            for g in (config.get("keywords") or {}).values()
            for i in (g or {}).get("items") or []}


def add_item(config: dict, group: str, keyword: str, source: str,
             weight: int, locked: bool, today: str) -> None:
    config["keywords"][group]["items"].append(
        {"keyword": keyword, "source": source, "added_date": today,
         "weight": weight, "locked": locked})


def doi_to_pmid(doi: str) -> str:
    """DOI → PMID（PubMed esearch [doi]），找不到返回空串。"""
    resp = requests.post(f"{EUTILS}/esearch.fcgi", data={
        "db": "pubmed", "term": f"{doi}[doi]", "retmax": 1, "retmode": "json",
    }, timeout=30)
    resp.raise_for_status()
    ids = resp.json().get("esearchresult", {}).get("idlist", [])
    return ids[0] if ids else ""


def fetch_crossref(doi: str) -> dict:
    """Crossref 兜底：取 title + abstract（去 JATS 标签）。"""
    resp = requests.get(f"{CROSSREF}/{doi}", timeout=30,
                        headers={"User-Agent": "research-radar/1.0"})
    resp.raise_for_status()
    msg = resp.json().get("message", {})
    title = " ".join(msg.get("title") or [])
    abstract = re.sub(r"<[^>]+>", " ", msg.get("abstract") or "")
    return {"title": title.strip(), "abstract": " ".join(abstract.split())}


def resolve_paper(raw_id: str) -> dict:
    """把单个 DOI/PMID 解析为 {title, abstract}；失败返回 None。"""
    raw_id = raw_id.strip()
    try:
        if raw_id.isdigit():
            papers = parse_efetch_xml(efetch([raw_id]))
            if papers:
                return {"title": papers[0]["title"], "abstract": papers[0]["abstract"]}
            return None
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", raw_id)
        pmid = doi_to_pmid(doi)
        if pmid:
            papers = parse_efetch_xml(efetch([pmid]))
            if papers:
                return {"title": papers[0]["title"], "abstract": papers[0]["abstract"]}
        return fetch_crossref(doi)
    except Exception as e:  # 网络/解析失败不让整批中断
        print(f"[跳过] 解析 {raw_id} 失败：{e}")
        return None


def mine_keywords_from_papers(papers: list, existing: set, max_new: int) -> list:
    """一次 AI 调用，从论文 title+abstract 提炼检索词（JSON 输出）。"""
    blocks = []
    for i, p in enumerate(papers, 1):
        abstract = " ".join((p.get("abstract") or "").split())[:1500]
        blocks.append(f"【论文 {i}】{p.get('title', '')}\n{abstract}")
    prompt = (PAPER_MINING_PROMPT
              .replace("{n}", str(len(blocks)))
              .replace("{max_new}", str(max_new))
              .replace("{existing}", "\n".join(sorted(existing)))
              .replace("{papers}", "\n\n".join(blocks)))
    result = call_model(prompt, response_format="json")
    kws = result.get("keywords") or []
    return [str(k).strip() for k in kws if str(k).strip()][:max_new]


def run(args) -> int:
    inbox = Path(args.inbox)
    processed = load_processed(Path(args.processed_log))
    files = sorted(f for f in inbox.glob("*.jsonl") if f.name not in processed) \
        if inbox.exists() else []
    if not files:
        print(f"没有需要处理的页面请求（目录 {inbox}）。")
        return 0

    kw_texts, paper_ids = [], []
    for f in files:
        for e in read_requests(f):
            if e.get("type") == "keywords" and e.get("text"):
                kw_texts.append(e["text"])
            elif e.get("type") == "papers":
                paper_ids.extend(e.get("ids") or [])
    paper_ids = list(dict.fromkeys(paper_ids))[:MAX_PAPERS]
    print(f"读取请求：关键词文本 {len(kw_texts)} 条，文献 id {len(paper_ids)} 个"
          f"（文件：{', '.join(f.name for f in files)}）")

    today = date.today().isoformat()
    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    profile = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
    seen = existing_norms(config) | load_rejected_norms(Path(args.archive))
    changed = False

    # 1) 手写关键词：种子级入库（weight=2, locked）
    for text in kw_texts:
        for kw in split_keywords(text):
            norm = normalize(kw)
            if norm in seen:
                print(f"[去重跳过] {kw}")
                continue
            seen.add(norm)
            group = pick_group(kw, profile, config)
            add_item(config, group, kw, "user_page", 2, True, today)
            changed = True
            print(f"[手写关键词入库] [{group}] + {kw} (weight=2, locked)")

    # 2) 文献提炼词：扩展级入库（weight=1, 可降权）
    papers = []
    for pid in paper_ids:
        info = resolve_paper(pid)
        if info and (info["title"] or info["abstract"]):
            papers.append(info)
        elif info is None:
            pass
        else:
            print(f"[跳过] {pid} 无标题与摘要")
    if papers:
        for kw in mine_keywords_from_papers(papers, seen, args.max_new):
            norm = normalize(kw)
            if not norm or norm in seen:
                print(f"[去重跳过] {kw}")
                continue
            seen.add(norm)
            group = pick_group(kw, profile, config)
            add_item(config, group, kw, "paper_page", 1, False, today)
            changed = True
            print(f"[文献提炼入库] [{group}] + {kw} (weight=1)")
    elif paper_ids:
        print("文献 id 均解析失败，跳过提炼。")

    if changed:
        dump_config_with_header(config, config_path)
        print(f"已回写 {config_path}（保留头部注释），新词今日检索即生效。")
    else:
        print("无新词入库。")

    mark_processed(Path(args.processed_log), [f.name for f in files])
    print(f"已记录处理标记到 {args.processed_log}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="消费日报网页提交的关键词/文献请求，次日生效")
    ap.add_argument("--inbox", default=str(ROOT / "input" / "keyword_requests"))
    ap.add_argument("--config", default=str(ROOT / "config" / "keyword_config.yaml"))
    ap.add_argument("--profile", default=str(ROOT / "input" / "seed_keywords.txt"))
    ap.add_argument("--archive", default=str(ROOT / "keyword_engine" / "archive_rejected.yaml"))
    ap.add_argument("--processed-log",
                    default=str(ROOT / "keyword_engine" / "processed_page_requests.log"))
    ap.add_argument("--max-new", type=int, default=MAX_NEW_FROM_PAPERS,
                    help="文献提炼词上限（默认 10）")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
