"""Phase 9：AI 相关性深度评分。

两批候选：① passed_filter=1 且 ai_score IS NULL 的论文（--max 控制成本，默认 20，
按 rule_score 从高到低取）；② 顶刊清单（config/top_journals.yaml）内期刊、
ai_score IS NULL 的论文（--max-tj 默认 30，期刊声望优先、同档日期降序；顶刊
不经关键词入池，需要 AI 语义分参与排序）。--month 限定发表月份（回溯合集用）；
--workers>1 线程池并行调用模型（IO 密集提速，写回仍在主线程逐条提交）。
Prompt 模板在 prompts/relevance_scoring_prompt.txt，
脚本只做变量填充。调用/JSON 解析失败 → 原始信息落盘 logs/ 并跳过该篇，不中断。
"""
import argparse
import json
import sys
from datetime import date, datetime
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


def select_candidates(conn, max_n: int, month: str = None) -> list:
    """第一批：关键词过滤通过、未评 AI 的论文（rule_score 降序）。

    month（YYYY-MM）：只取该月发表的论文（回溯合集用，避免全库候选挤占额度）。"""
    sql = ("SELECT p.paper_id, p.title, p.abstract FROM papers p "
           "JOIN paper_scores s ON p.paper_id = s.paper_id "
           "WHERE s.passed_filter = 1 AND s.ai_score IS NULL ")
    params = []
    if month:
        sql += "AND p.date LIKE ? "
        params.append(month + "%")
    sql += "ORDER BY s.rule_score DESC LIMIT ?"
    params.append(max_n)
    return conn.execute(sql, params).fetchall()


def select_top_journal_candidates(conn, max_tj: int, journals_path=None,
                                  month: str = None) -> list:
    """第二批：顶刊清单内期刊、未评 AI 的论文。

    顶刊论文不经关键词入池，需要 AI 语义分参与四维排序。按期刊声望优先
    （Nature/Science/Cell 主刊 10 分档先评），同档按日期降序（最新的优先）。
    month（YYYY-MM）：只取该月发表的论文（回溯合集用）。
    """
    from crawler.top_journals import journal_names
    from ranking.scoring import journal_score
    names = journal_names(journals_path)
    if not names or max_tj <= 0:
        return []
    marks = ",".join("?" * len(names))
    sql = ("SELECT p.paper_id, p.title, p.abstract, p.journal, p.date FROM papers p "
           "JOIN paper_scores s ON p.paper_id = s.paper_id "
           f"WHERE s.ai_score IS NULL AND LOWER(p.journal) IN ({marks})")
    params = list(names)
    if month:
        sql += " AND p.date LIKE ?"
        params.append(month + "%")
    rows = conn.execute(sql, params).fetchall()
    rows.sort(key=lambda r: (journal_score(r["journal"]), r["date"] or ""),
              reverse=True)
    return rows[:max_tj]


def select_ceiling_candidates(conn, max_n: int, min_ceiling: float = 7.0,
                              month: str = None) -> list:
    """Must Read-only 模式：只评"理论可达 Must Read"的论文。

    AI 满分也只贡献 0.3×10=3.0 分，非 AI 三维天花板太低的论文即使 AI 满分
    也到不了 Must Read 7 分线，不值得花 AI 调用。这里把 ai_score 强制为 10
    计算 ceiling 总分，只保留 ceiling ≥ min_ceiling 的候选（两通道合并：
    关键词初筛通过 ∪ 顶刊）。
    """
    from crawler.top_journals import journal_names
    from ranking.scoring import compute_scores, load_kw_config, load_weights
    from ranking.trend_signals import build_trend_ctx

    names = journal_names()
    marks = ",".join("?" * len(names))
    sql = ("SELECT p.paper_id, p.title, p.abstract, p.journal, p.date, "
           "s.rule_score, s.passed_filter FROM papers p "
           "JOIN paper_scores s ON p.paper_id = s.paper_id "
           f"WHERE s.ai_score IS NULL AND (s.passed_filter = 1 "
           f"OR LOWER(p.journal) IN ({marks}))")
    params = list(names)
    if month:
        sql += " AND p.date LIKE ?"
        params.append(month + "%")
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return []
    kw = load_kw_config()
    w = load_weights()
    ctx = build_trend_ctx(conn, date.today().isoformat(), offline=True)
    for r in rows:
        r["ai_score"] = 10  # AI 满分上限 → ceiling
    scored = {e["paper_id"]: e["total_score"]
              for e in compute_scores(rows, w, kw, include_vetoed=True,
                                      trend_ctx=ctx)}
    keep = [r for r in rows if scored.get(r["paper_id"], 0) >= min_ceiling]
    keep.sort(key=lambda r: scored[r["paper_id"]], reverse=True)
    return keep[:max_n]


def run(conn, max_n: int, max_tj: int = 30, month: str = None,
        workers: int = 1, min_ceiling: float = None) -> dict:
    """分两批调用 AI 并写回 paper_scores，返回统计。

    workers>1 时用线程池并行调用模型（IO 密集；回溯合集提速用），
    数据库写回始终在主线程按完成顺序逐条提交（崩溃不丢进度）。
    min_ceiling 设定时改用 ceiling 预筛（Must Read-only 模式，两通道合并，
    忽略 max_tj）。
    """
    if min_ceiling is not None:
        rows = list(select_ceiling_candidates(conn, max_n, min_ceiling, month))
        print(f"[ceiling] ≥{min_ceiling} 候选 {len(rows)} 篇", flush=True)
    else:
        rows = list(select_candidates(conn, max_n, month))
        tj_rows = list(select_top_journal_candidates(conn, max_tj, month=month))
        seen = {r["paper_id"] for r in rows}
        rows += [r for r in tj_rows if r["paper_id"] not in seen]
    template = (ROOT / "prompts" / "relevance_scoring_prompt.txt").read_text(encoding="utf-8")
    profile = load_profile_summary()

    def analyze(r):
        prompt = build_prompt(template, profile, r["title"], r["abstract"])
        return r["paper_id"], call_model(prompt, response_format="json")

    def write_back(paper_id, result):
        conn.execute(
            "UPDATE paper_scores SET ai_score=?, category=?, reason=?, "
            "title_cn=?, one_line_summary_cn=?, abstract_cn=?, reproducibility=? WHERE paper_id=?",
            (float(result.get("relevance_score", 0)), result.get("category", ""),
             result.get("reason", ""), result.get("title_cn", ""),
             result.get("one_line_summary_cn", ""),
             result.get("abstract_cn", ""),
             json.dumps(result.get("reproducibility") or {}, ensure_ascii=False),
             paper_id),
        )
        conn.commit()

    done = failed = 0
    total = len(rows)
    if workers > 1 and total:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(analyze, r): r for r in rows}
            for fut in as_completed(futs):
                r = futs[fut]
                try:
                    paper_id, result = fut.result()
                except Exception as e:  # noqa: BLE001
                    dump_failure(r["paper_id"], e)
                    failed += 1
                    print(f"[跳过] {r['paper_id']} AI 调用/解析失败：{e}", flush=True)
                    continue
                write_back(paper_id, result)
                done += 1
                if done % 20 == 0 or done == total:
                    print(f"[进度] {done + failed}/{total}（成功 {done}）", flush=True)
    else:
        for r in rows:
            try:
                paper_id, result = analyze(r)
            except Exception as e:  # noqa: BLE001
                dump_failure(r["paper_id"], e)
                failed += 1
                print(f"[跳过] {r['paper_id']} AI 调用/解析失败：{e}")
                continue
            write_back(paper_id, result)
            done += 1
            print(f"[完成] {paper_id} ai_score={result.get('relevance_score')}")
    return {"candidates": total, "done": done, "failed": failed}


def main():
    ap = argparse.ArgumentParser(description="AI 相关性深度评分")
    ap.add_argument("--max", type=int, default=20, help="本次最多评分篇数（默认 20，控制成本）")
    ap.add_argument("--max-tj", type=int, default=30, help="顶刊候选本次最多评分篇数（默认 30）")
    ap.add_argument("--month", default=None,
                    help="只评该月发表的论文 YYYY-MM（回溯合集用，默认不限）")
    ap.add_argument("--workers", type=int, default=1,
                    help="并行调用模型的线程数（默认 1 串行；回溯合集可设 8）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--min-ceiling", type=float, default=None,
                    help="Must Read-only 模式：只评 ceiling 总分≥该值的候选（如 7），忽略 --max-tj")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    stats = run(conn, args.max, args.max_tj, month=args.month,
                workers=args.workers, min_ceiling=args.min_ceiling)
    conn.close()
    print(f"AI 评分完成：候选 {stats['candidates']} / 成功 {stats['done']} / 失败 {stats['failed']}")


if __name__ == "__main__":
    main()
