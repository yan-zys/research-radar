"""Phase 12：用户反馈学习闭环。

读取 input/user_feedback/YYYY-MM-DD.jsonl 中的用户反馈（paper_id + rating）：
- 负反馈（bad）：对该论文命中的词条降权——weight>1 且非 locked 的词条 weight -= 1
  （下限 1）；locked=true 的种子词无论负反馈如何不得降权，跳过并打印说明；
  回写 config/keyword_config.yaml 时保留文件头部注释；
- 正反馈（good）：调用 AI 从这些论文 title+abstract 中提炼关键词库之外的候选新词
  （每日 ≤ max_new 个，A/B/C 分级 + 中文理由），以 status: pending / source: feedback
  追加到 keyword_engine/output_candidates.yaml——必须经过 review_candidates.py
  人工审核才能入库，绝不自动 merge；与现有候选及 archive_rejected.yaml 去重；
- 已处理的反馈文件名记录到 feedback/processed.log，避免重复处理；
- 无反馈文件时打印提示并正常退出（退出码 0）；无 good 反馈时不调用 AI。
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from keyword_engine.expand_keywords import LEVELS, load_existing_keywords, normalize  # noqa: E402
from processing.keyword_filter import match_keywords  # noqa: E402

MAX_CHARS_PER_PAPER = 1500
MIN_ITEM_WEIGHT = 1


def load_processed(log_path: Path) -> set:
    """读取已处理反馈文件名集合。"""
    if not log_path.exists():
        return set()
    return {l.strip() for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()}


def mark_processed(log_path: Path, names) -> None:
    """把处理完的反馈文件名追加到 processed.log。"""
    if not names:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        for name in names:
            f.write(f"{name}\n")


def read_feedback(path: Path) -> list:
    """读取一个 jsonl 反馈文件，跳过空行与坏行。"""
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


def select_files(feedback_dir: Path, day: str, all_files: bool, processed: set) -> list:
    """选出本次要处理的反馈文件（不存在或已处理的跳过）。"""
    if all_files:
        files = sorted(feedback_dir.glob("*.jsonl"))
    else:
        files = [feedback_dir / f"{day}.jsonl"]
    return [f for f in files if f.exists() and f.name not in processed]


def dump_config_with_header(config: dict, path: Path) -> None:
    """回写 keyword_config.yaml，保留原文件头部的 # 注释块。"""
    header = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#") or not line.strip():
                header.append(line)
            else:
                break
    body = yaml.dump(config, allow_unicode=True, sort_keys=False)
    text = ("\n".join(header) + "\n" if header else "") + body
    path.write_text(text, encoding="utf-8")


def apply_negative_feedback(config: dict, match: dict, paper_id: str) -> bool:
    """对 bad 论文命中的词条降权：weight>1 且未 locked → weight -= 1。返回是否有改动。"""
    changed = False
    for hit in match["hits"]:
        items = config["keywords"][hit["group"]]["items"]
        item = next(i for i in items if str(i["keyword"]) == hit["keyword"])
        if item.get("locked"):
            print(f"[锁定跳过] {hit['keyword']} 是 locked 种子词，不随负反馈降权")
            continue
        w = int(item.get("weight", 1))
        if w <= MIN_ITEM_WEIGHT:
            print(f"[下限跳过] {hit['keyword']} 已是 weight={MIN_ITEM_WEIGHT}，不再降权")
            continue
        item["weight"] = w - 1
        changed = True
        print(f"[降权] {hit['keyword']}: weight {w} -> {w - 1}（bad 反馈 {paper_id}）")
    return changed


def fetch_papers(conn, entries: list) -> list:
    """按 paper_id 关联 papers 表取 title+abstract；找不到的反馈跳过。"""
    papers = []
    for e in entries:
        pid = e.get("paper_id")
        row = conn.execute("SELECT title, abstract FROM papers WHERE paper_id = ?",
                           (pid,)).fetchone()
        if row is None:
            print(f"[跳过] 数据库中找不到论文 {pid}（rating={e.get('rating')}）")
            continue
        papers.append({"paper_id": pid, "rating": e.get("rating"),
                       "title": row["title"] or "", "abstract": row["abstract"] or ""})
    return papers


def mine_positive_candidates(papers: list, config_path: Path, candidates_path: Path,
                             archive_path: Path, max_new: int) -> list:
    """对 good 论文调用 AI 提炼候选新词，去重后返回待追加候选（不写文件）。"""
    template = (ROOT / "prompts" / "feedback_mining_prompt.txt").read_text(encoding="utf-8")
    blocks = []
    for i, p in enumerate(papers, 1):
        abstract = " ".join(p["abstract"].split())[:MAX_CHARS_PER_PAPER]
        blocks.append(f"【论文 {i}】{p['title']}\n{abstract}")
    existing = sorted(load_existing_keywords(config_path))
    prompt = (template
              .replace("{{papers}}", "\n\n".join(blocks))
              .replace("{{n}}", str(len(blocks)))
              .replace("{{existing_keywords}}", "\n".join(existing))
              .replace("{{max_new}}", str(max_new)))

    result = call_model(prompt, response_format="json")
    reasoning = result.get("reasoning") or {}

    # 去重集合：关键词库现有词条 + 现有候选 + 已拒绝归档（normalize 同 keyword_engine）
    seen = {normalize(k) for k in existing}
    for path in (candidates_path, archive_path):
        if path.exists():
            for c in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                seen.add(normalize(c["keyword"]))

    candidates = []
    for field, level in LEVELS:
        for kw in result.get(field) or []:
            norm = normalize(kw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            candidates.append({"keyword": str(kw), "level": level, "source": "feedback",
                               "reason": reasoning.get(kw, ""), "status": "pending"})
            if len(candidates) >= max_new:
                break
        if len(candidates) >= max_new:
            break
    return candidates


def run(args) -> int:
    """主流程：读反馈 → 负反馈降权 → 正反馈挖候选 → 记录已处理。返回退出码。"""
    feedback_dir = Path(args.feedback_dir)
    day = args.date or (date.today() - timedelta(days=1)).isoformat()
    processed = load_processed(Path(args.processed_log))
    files = select_files(feedback_dir, day, args.all, processed)
    if not files:
        print(f"没有需要处理的反馈文件（日期 {day}，目录 {feedback_dir}）。")
        return 0

    entries = []
    for f in files:
        entries.extend(read_feedback(f))
    print(f"读取反馈 {len(entries)} 条（文件：{', '.join(f.name for f in files)}）")

    conn = get_conn(args.db)
    init_db(conn)
    papers = fetch_papers(conn, entries)
    conn.close()

    config_path = Path(args.config)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    bad = [p for p in papers if p["rating"] == "bad"]
    good = [p for p in papers if p["rating"] == "good"]

    changed = False
    for p in bad:
        m = match_keywords(p["title"], p["abstract"], config)
        if not m["hits"]:
            print(f"[无命中] {p['paper_id']} 未命中任何词条，无需降权")
            continue
        changed |= apply_negative_feedback(config, m, p["paper_id"])
    if changed:
        dump_config_with_header(config, config_path)
        print(f"已回写 {config_path}（保留头部注释）")
    else:
        print("关键词权重无变化。")

    if good:
        candidates = mine_positive_candidates(good, config_path, Path(args.candidates),
                                              Path(args.archive), args.max_new)
        if candidates:
            out = Path(args.candidates)
            existing = (yaml.safe_load(out.read_text(encoding="utf-8"))
                        if out.exists() else []) or []
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(yaml.dump(existing + candidates, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
            print(f"新增候选 {len(candidates)} 条（status: pending，需人工审核），已追加到 {out}")
        else:
            print("AI 未提炼出新的候选词。")
    else:
        print("无 good 反馈，不调用 AI 挖掘候选词。")

    mark_processed(Path(args.processed_log), [f.name for f in files])
    print(f"已记录处理标记到 {args.processed_log}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Phase 12：用户反馈学习闭环")
    ap.add_argument("--date", default=None, help="反馈日期 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--all", action="store_true", help="处理全部未处理的反馈文件")
    ap.add_argument("--feedback-dir", default=str(ROOT / "input" / "user_feedback"))
    ap.add_argument("--config", default=str(ROOT / "config" / "keyword_config.yaml"))
    ap.add_argument("--candidates", default=str(ROOT / "keyword_engine" / "output_candidates.yaml"))
    ap.add_argument("--archive", default=str(ROOT / "keyword_engine" / "archive_rejected.yaml"))
    ap.add_argument("--processed-log", default=str(ROOT / "feedback" / "processed.log"))
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--max-new", type=int, default=5, help="每日新增候选上限（默认 5）")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
