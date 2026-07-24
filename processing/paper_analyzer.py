"""Phase 9：AI 相关性深度评分。

只对 passed_filter=1 且 ai_score IS NULL 的论文调用 AI（--max 控制成本，默认 20，
按 rule_score 从高到低取）。Prompt 模板在 prompts/relevance_scoring_prompt.txt，
脚本只做变量填充。调用/JSON 解析失败 → 原始信息落盘 logs/ 并跳过该篇，不中断。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402


def load_profile_summary(path=None) -> str:
    """从 input/seed_keywords.txt 概括科研画像（方向/关键词/方法/物种/工具/排除）。"""
    p = Path(path) if path else ROOT / "input" / "seed_keywords.txt"
    profile = yaml.safe_load(p.read_text(encoding="utf-8"))
    parts = []
    for label, key in (("研究方向", "research_interest"), ("关键词", "keywords"),
                       ("方法", "methods"), ("物种", "species"), ("工具", "tools"),
                       ("排除", "exclude")):
        items = profile.get(key) or []
        if items:
            parts.append(f"{label}: {', '.join(str(i) for i in items)}")
    return "\n".join(parts)


def build_prompt(template: str, profile_summary: str, title: str, abstract: str) -> str:
    """模板变量填充（py 里不写死 prompt 内容）。"""
    return (template.replace("{{profile}}", profile_summary)
                    .replace("{{title}}", title or "")
                    .replace("{{abstract}}", abstract or ""))


def dump_failure(paper_id: str, content) -> None:
    """失败详情落盘 logs/，便于诊断。"""
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    safe = paper_id.replace("/", "_").replace(":", "_")
    ts = datetime.now().strftime("%H%M%S")
    (logs / f"ai_fail_{safe}_{ts}.txt").write_text(str(content), encoding="utf-8")


def run(conn, max_n: int) -> dict:
    """逐篇调用 AI 并写回 paper_scores，返回统计。"""
    rows = conn.execute(
        "SELECT p.paper_id, p.title, p.abstract FROM papers p "
        "JOIN paper_scores s ON p.paper_id = s.paper_id "
        "WHERE s.passed_filter = 1 AND s.ai_score IS NULL "
        "ORDER BY s.rule_score DESC LIMIT ?",
        (max_n,),
    ).fetchall()
    template = (ROOT / "prompts" / "relevance_scoring_prompt.txt").read_text(encoding="utf-8")
    profile = load_profile_summary()
    done = failed = 0
    for r in rows:
        prompt = build_prompt(template, profile, r["title"], r["abstract"])
        try:
            result = call_model(prompt, response_format="json")
        except Exception as e:  # noqa: BLE001
            dump_failure(r["paper_id"], e)
            failed += 1
            print(f"[跳过] {r['paper_id']} AI 调用/解析失败：{e}")
            continue
        conn.execute(
            "UPDATE paper_scores SET ai_score=?, category=?, reason=?, "
            "one_line_summary_cn=?, abstract_cn=?, reproducibility=? WHERE paper_id=?",
            (float(result.get("relevance_score", 0)), result.get("category", ""),
             result.get("reason", ""), result.get("one_line_summary_cn", ""),
             result.get("abstract_cn", ""),
             json.dumps(result.get("reproducibility") or {}, ensure_ascii=False),
             r["paper_id"]),
        )
        conn.commit()
        done += 1
        print(f"[完成] {r['paper_id']} ai_score={result.get('relevance_score')}")
    return {"candidates": len(rows), "done": done, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="AI 相关性深度评分")
    ap.add_argument("--max", type=int, default=20, help="本次最多评分篇数（默认 20，控制成本）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    stats = run(conn, args.max)
    conn.close()
    print(f"AI 评分完成：候选 {stats['candidates']} / 成功 {stats['done']} / 失败 {stats['failed']}")


if __name__ == "__main__":
    main()
