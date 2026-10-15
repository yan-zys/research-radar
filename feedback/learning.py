"""Phase 12：用户反馈学习闭环。

读取 input/user_feedback/YYYY-MM-DD.jsonl 中的用户反馈（paper_id + rating，可选 reason）。
默认处理目录下全部未处理文件（--date 可限定单日）；同一文件内同一 paper_id
多条记录时只生效最后一条（网页多次改判以最后一次为准）：
- 负反馈（bad）：对该论文命中的词条降权——weight>1 且 locked: false 的词条按原因分档降权：
  reason 含「方向不符」/「物种不符」→ weight -= 2，其余 bad（含无 reason）→ weight -= 1
  （下限 1）；锁定词（默认 locked，含全部种子词与手写词）无论负反馈如何不得降权，
  跳过并打印说明；回写 config/keyword_config.yaml 用规范格式（固定头注释+紧凑词条）；
- 医学噪声聚合：reason 含「医学」的 bad 论文当批累计 ≥ MEDICAL_EXCLUDE_THRESHOLD 篇时，
  调用 AI 从这些论文 title+abstract 提炼 ≤5 个英文排除词，与 config 现有 negative、
  现有候选及 archive_rejected 去重后，以 level: exclude / status: pending 追加到
  keyword_engine/output_candidates.yaml——经 review + merge_to_config 批准后才进入
  config 的 negative 排除词列表，绝不自动生效；
- 正反馈（good / star）：调用 AI 从这些论文 title+abstract 中提炼关键词库之外的候选新词
  （每日 ≤ max_new 个，A/B/C 分级 + 中文理由），以 status: pending / source: feedback
  追加到 keyword_engine/output_candidates.yaml——必须经过 review_candidates.py
  人工审核才能入库，绝不自动 merge；与现有候选及 archive_rejected.yaml 去重；
  star（收藏）视为正反馈，与 good 同等处理；read（已读）/ok 及其余未知 rating 跳过不计；
- 已处理的反馈文件名记录到 feedback/processed.log，避免重复处理；
- 无反馈文件时打印提示并正常退出（退出码 0）；无 good 反馈时不调用 AI。
"""
import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from keyword_engine.expand_keywords import LEVELS, load_existing_keywords, normalize  # noqa: E402
from processing.keyword_filter import item_keyword, match_keywords  # noqa: E402

MAX_CHARS_PER_PAPER = 1500
MIN_ITEM_WEIGHT = 1
# 原因分档：reason 命中强原因 → 一次降 2；其余 bad → 降 1
STRONG_REASON_KEYWORDS = ("方向不符", "物种不符")
MEDICAL_REASON_KEYWORD = "医学"
# 当批「医学」原因的 bad 论文达到该篇数才触发一次排除词提炼（避免偶发噪声反复调 AI）
MEDICAL_EXCLUDE_THRESHOLD = 2
MAX_MEDICAL_EXCLUDE_WORDS = 5

MEDICAL_EXCLUDE_PROMPT = """以下是用户反馈为「不相关」且原因标注为医学/临床噪声的 {n} 篇论文：

{papers}

请从中提炼最多 {max_words} 个可用于过滤此类噪声的英文排除词或短语（小写、尽量通用，
如 clinical trial、patient cohort 这类通用噪声词，避免疾病专名过窄的词）。
已有的排除词列表如下，不要重复：

{existing}

只输出 JSON：{{"exclude": ["...", "..."]}}"""


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
    """读取一个 jsonl 反馈文件，跳过空行与坏行。

    同一 paper_id 在文件内出现多条记录时只保留最后一条（用户在网页上多次
    改判同一篇论文，以最后一次选择为准），位置保持首次出现处的顺序。"""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[跳过] {path.name} 中的坏行: {line[:80]}")
    deduped, pos = [], {}
    for e in entries:
        pid = e.get("paper_id")
        if not pid:
            deduped.append(e)
            continue
        if pid in pos:
            deduped[pos[pid]] = e
        else:
            pos[pid] = len(deduped)
            deduped.append(e)
    if len(deduped) != len(entries):
        print(f"[去重] {path.name}：{len(entries)} 条记录按 paper_id 去重后 "
              f"剩 {len(deduped)} 条（同一论文多次反馈以最后一次为准）")
    return deduped


def select_files(feedback_dir: Path, day: str, all_files: bool, processed: set) -> list:
    """选出本次要处理的反馈文件（不存在或已处理的跳过）。

    all_files=True 时处理目录下全部未处理文件（默认行为，避免反馈文件在
    当日运行之后才同步进来而成为永远不会被处理的孤儿）；否则只处理 day 当天。"""
    if all_files:
        files = sorted(feedback_dir.glob("*.jsonl"))
    else:
        files = [feedback_dir / f"{day}.jsonl"]
    return [f for f in files if f.exists() and f.name not in processed]


CONFIG_HEADER = """# 关键词库（crawler 检索 / filter 初筛 / scoring 评分唯一读取的配置）
#
# 计分：命中加分 = 组权重 × 词条权重
#   core 15 → 30 分/词（A0：任一命中直接进 AI 评分）
#   core_broad 15 → 30 分/词（须组学锚点 + 演化锚点共现才 A0）
#   concept 10 → 20 分/词 | methods 8 → 16 分/词 | species 5 → 10 分/词 | topics 2 → 4 分/词
# 词条写法：纯字符串 = 默认（weight 2、locked、参与召回）；
#   例外写流式 dict，只标非默认字段：- {keyword: x, weight: 1, locked: false, recall: false}
#   weight: 词条权重（默认 2）；locked: false 才可被负反馈降权（默认锁定，仅人工删改）；
#   recall: false 只加分，不参与检索召回与初筛门（species 整组如此）"""

_YAML_SPECIAL = re.compile(r"[:#\[\]{}&,*!|>'\"%@`]|^\s|\s$")


def _yaml_scalar(s: str) -> str:
    """标量输出：含 YAML 特殊字符时用双引号包裹，否则原样输出。"""
    return json.dumps(s, ensure_ascii=False) if _YAML_SPECIAL.search(s) else s


def _format_item(item) -> str:
    """词条紧凑写法：默认（weight 2 / locked / 召回）→ 纯字符串；例外 → 流式 dict。"""
    if isinstance(item, str):
        return f"- {_yaml_scalar(item)}"
    parts = [f"keyword: {_yaml_scalar(str(item['keyword']))}"]
    if int(item.get("weight", 2)) != 2:
        parts.append(f"weight: {int(item['weight'])}")
    if not item.get("locked", True):
        parts.append("locked: false")
    if item.get("recall") is False:
        parts.append("recall: false")
    return "- {" + ", ".join(parts) + "}"


def dump_config_with_header(config: dict, path: Path) -> None:
    """按规范格式回写 keyword_config.yaml（固定头注释 + 紧凑词条）。"""
    lines = [CONFIG_HEADER, "keywords:"]
    for group, gdata in (config.get("keywords") or {}).items():
        gdata = gdata or {}
        lines.append(f"  {group}:")
        lines.append(f"    weight: {int(gdata.get('weight', 1))}")
        if gdata.get("recall") is False:
            lines.append("    recall: false")
        items = gdata.get("items") or []
        if items:
            lines.append("    items:")
            for item in items:
                lines.append(f"      {_format_item(item)}")
        else:
            lines.append("    items: []")
    lines.append("negative:")
    for w in config.get("negative") or []:
        lines.append(f"- {_yaml_scalar(str(w))}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_negative_feedback(config: dict, match: dict, paper_id: str, step: int = 1) -> bool:
    """对 bad 论文命中的词条降权：weight>1 且 locked: false → weight -= step（下限 MIN_ITEM_WEIGHT）。

    step 按 reason 分档：「方向不符」/「物种不符」为 2，其余 bad 为 1。
    纯字符串词条与未显式标 locked: false 的 dict 均视为锁定，跳过。返回是否有改动。"""
    changed = False
    for hit in match["hits"]:
        items = config["keywords"][hit["group"]]["items"]
        item = next(i for i in items if item_keyword(i) == hit["keyword"])
        if isinstance(item, str) or item.get("locked", True):
            print(f"[锁定跳过] {hit['keyword']} 是锁定词（默认 locked），不随负反馈降权")
            continue
        w = int(item.get("weight", 2))
        if w <= MIN_ITEM_WEIGHT:
            print(f"[下限跳过] {hit['keyword']} 已是 weight={MIN_ITEM_WEIGHT}，不再降权")
            continue
        new_w = max(MIN_ITEM_WEIGHT, w - step)
        item["weight"] = new_w
        changed = True
        print(f"[降权] {hit['keyword']}: weight {w} -> {new_w}（bad 反馈 {paper_id}，step={step}）")
    return changed


def fetch_papers(conn, entries: list) -> list:
    """按 paper_id 关联 papers 表取 title+abstract；找不到的反馈跳过。reason 原样带出。"""
    papers = []
    for e in entries:
        pid = e.get("paper_id")
        row = conn.execute("SELECT title, abstract FROM papers WHERE paper_id = ?",
                           (pid,)).fetchone()
        if row is None:
            print(f"[跳过] 数据库中找不到论文 {pid}（rating={e.get('rating')}）")
            continue
        papers.append({"paper_id": pid, "rating": e.get("rating"),
                       "reason": e.get("reason") or "",
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


def mine_medical_exclude_candidates(papers: list, config: dict, candidates_path: Path,
                                    archive_path: Path) -> list:
    """对「医学·临床噪声」bad 论文调 AI 提炼英文排除词。

    与 config 现有 negative、现有候选、archive_rejected 去重后返回候选（不写文件），
    每条形如 {"keyword": .., "level": "exclude", "source": "feedback_reason",
    "status": "pending"}——须经 review + merge_to_config 批准才进 config["negative"]。"""
    blocks = []
    for i, p in enumerate(papers, 1):
        abstract = " ".join(p["abstract"].split())[:MAX_CHARS_PER_PAPER]
        blocks.append(f"【论文 {i}】{p['title']}\n{abstract}")
    existing_negative = config.get("negative") or []
    prompt = (MEDICAL_EXCLUDE_PROMPT
              .replace("{n}", str(len(blocks)))
              .replace("{papers}", "\n\n".join(blocks))
              .replace("{max_words}", str(MAX_MEDICAL_EXCLUDE_WORDS))
              .replace("{existing}", "\n".join(str(w) for w in existing_negative)))

    result = call_model(prompt, response_format="json")

    seen = {normalize(str(w)) for w in existing_negative}
    for path in (candidates_path, archive_path):
        if path.exists():
            for c in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                seen.add(normalize(c["keyword"]))

    candidates = []
    for kw in result.get("exclude") or []:
        norm = normalize(str(kw))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        candidates.append({"keyword": str(kw), "level": "exclude",
                           "source": "feedback_reason",
                           "reason": "医学噪声负反馈聚类", "status": "pending"})
        if len(candidates) >= MAX_MEDICAL_EXCLUDE_WORDS:
            break
    return candidates


def run(args) -> int:
    """主流程：读反馈 → 负反馈降权 → 正反馈挖候选 → 记录已处理。返回退出码。"""
    feedback_dir = Path(args.feedback_dir)
    day = args.date or (date.today() - timedelta(days=1)).isoformat()
    processed = load_processed(Path(args.processed_log))
    # 默认（未指定 --date）处理全部未处理文件，保证反馈次日必被学习；
    # --date 仅用于补算/重算指定日期
    files = select_files(feedback_dir, day, args.all or not args.date, processed)
    if not files:
        print(f"没有需要处理的反馈文件（目录 {feedback_dir}）。")
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
    # star（收藏）视为正反馈，与 good 同等参与候选词挖掘；read/ok 等其余 rating 跳过不计
    good = [p for p in papers if p["rating"] in ("good", "star")]

    changed = False
    for p in bad:
        m = match_keywords(p["title"], p["abstract"], config)
        if not m["hits"]:
            print(f"[无命中] {p['paper_id']} 未命中任何词条，无需降权")
            continue
        step = 2 if any(k in p["reason"] for k in STRONG_REASON_KEYWORDS) else 1
        changed |= apply_negative_feedback(config, m, p["paper_id"], step=step)
    if changed:
        dump_config_with_header(config, config_path)
        print(f"已回写 {config_path}（保留头部注释）")
    else:
        print("关键词权重无变化。")

    # 医学噪声聚合：当批 reason 含「医学」的 bad 达到阈值 → 提炼英文排除词候选
    medical_bad = [p for p in bad if MEDICAL_REASON_KEYWORD in p["reason"]]
    medical_candidates = []
    if len(medical_bad) >= MEDICAL_EXCLUDE_THRESHOLD:
        medical_candidates = mine_medical_exclude_candidates(
            medical_bad, config, Path(args.candidates), Path(args.archive))
        if medical_candidates:
            print(f"医学噪声聚合：{len(medical_bad)} 篇 bad → "
                  f"提炼排除词候选 {len(medical_candidates)} 个")
        else:
            print("医学噪声聚合：AI 未提炼出新的排除词。")
    elif medical_bad:
        print(f"医学噪声 bad 仅 {len(medical_bad)} 篇"
              f"（阈值 {MEDICAL_EXCLUDE_THRESHOLD}），本批不提炼排除词。")

    candidates = list(medical_candidates)
    if good:
        mined = mine_positive_candidates(good, config_path, Path(args.candidates),
                                         Path(args.archive), args.max_new)
        if mined:
            print(f"AI 从 good 反馈提炼候选 {len(mined)} 条。")
            candidates.extend(mined)
        else:
            print("AI 未提炼出新的候选词。")
    else:
        print("无 good 反馈，不调用 AI 挖掘候选词。")

    if candidates:
        out = Path(args.candidates)
        existing = (yaml.safe_load(out.read_text(encoding="utf-8"))
                    if out.exists() else []) or []
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(yaml.dump(existing + candidates, allow_unicode=True, sort_keys=False),
                       encoding="utf-8")
        print(f"新增候选 {len(candidates)} 条（status: pending，需人工审核），已追加到 {out}")

    mark_processed(Path(args.processed_log), [f.name for f in files])
    print(f"已记录处理标记到 {args.processed_log}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Phase 12：用户反馈学习闭环")
    ap.add_argument("--date", default=None,
                    help="只处理该日期的反馈文件 YYYY-MM-DD（默认处理全部未处理文件）")
    ap.add_argument("--all", action="store_true",
                    help="处理全部未处理的反馈文件（现为默认行为，保留仅为兼容旧脚本）")
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
