"""四维加权打分与每日 Top 推荐。

各维度归一化到 0-10 后按 config/scoring.yaml 权重加权（total_score 0-10）：
- research_relevance：rule_score 除以候选池最大值 ×10
- ai_semantic_relevance：ai_score（缺失按 5；已评且 <3 的论文被 AI 否决，不参与推荐）
- journal_influence：内置期刊分档 dict
- trend_value：concept 组命中得分归一化（复用 keyword_filter）
Top 15 全部写入 recommendations，不再因总分低剔除
（grade：≥7 Must Read / ≥5 Important / 其余 Relate；同日期先清空旧记录再写入）。
打印 Top15 简表。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import group_scores, match_keywords  # noqa: E402


def journal_score(journal: str) -> float:
    """内置期刊分档：Nature/Science/Cell 及子刊=10，eLife/PLOS=7，bioRxiv=4，默认=5。"""
    j = (journal or "").strip().lower()
    if "biorxiv" in j or "arxiv" in j:
        return 4.0
    if j.startswith(("nature", "science", "cell")) or j.startswith(("nat ", "nat.")):
        return 10.0
    if "elife" in j or "plos" in j:
        return 7.0
    return 5.0


def grade_of(total: float) -> str:
    """等级分档：≥7 Must Read / ≥5 Important / 其余 Relate（全部入选，不剔除）。"""
    if total >= 7:
        return "Must Read"
    if total >= 5:
        return "Important"
    return "Relate"


def load_weights(path=None) -> dict:
    """加载 config/scoring.yaml 的 weights。"""
    p = Path(path) if path else ROOT / "config" / "scoring.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("weights") or {}


def load_kw_config() -> dict:
    """加载 keyword_config.yaml（trend 维度复用其命中逻辑）。"""
    return yaml.safe_load((ROOT / "config" / "keyword_config.yaml").read_text(encoding="utf-8"))


def compute_scores(rows, weights: dict, kw_config: dict, ai_veto: float = 3.0) -> list:
    """对候选论文计算四维得分与 total_score（rows 为 papers JOIN paper_scores）。

    AI 否决：ai_score 已评且低于 ai_veto（默认 3）的论文直接剔除，
    避免规则分高但 AI 判定无关的论文混入推荐。
    """
    enriched = []
    for r in rows:
        if r["ai_score"] is not None and float(r["ai_score"]) < ai_veto:
            continue
        agg = group_scores(match_keywords(r["title"], r["abstract"], kw_config))
        enriched.append({
            "paper_id": r["paper_id"], "title": r["title"], "journal": r["journal"],
            "rule_score": r["rule_score"] or 0,
            "ai_score": r["ai_score"] if r["ai_score"] is not None else 5,
            "trend_raw": agg.get("concept", 0),
        })
    max_rule = max((e["rule_score"] for e in enriched), default=0) or 1
    max_trend = max((e["trend_raw"] for e in enriched), default=0) or 1
    for e in enriched:
        dims = {
            "research_relevance": e["rule_score"] / max_rule * 10,
            "ai_semantic_relevance": float(e["ai_score"]),
            "journal_influence": journal_score(e["journal"]),
            "trend_value": e["trend_raw"] / max_trend * 10,
        }
        e["total_score"] = round(sum(dims[k] * weights.get(k, 0) for k in dims), 2)
        e["grade"] = grade_of(e["total_score"])
    return enriched


def run(conn, weights: dict, kw_config: dict, run_date=None, top_n: int = 15) -> list:
    """对 passed_filter=1 的论文打分，Top N 写入 recommendations（同日期先清空）。

    30 天去重：近 30 天内已进过 recommendations 的论文不再参与当日候选。
    Top N 全部入选，不再因 total_score < 5 剔除。
    """
    if run_date is None:
        d = date.today().isoformat()
    elif isinstance(run_date, str):
        d = run_date
    else:
        d = run_date.isoformat()
    rows = conn.execute(
        "SELECT p.paper_id, p.title, p.abstract, p.journal, s.rule_score, s.ai_score "
        "FROM papers p JOIN paper_scores s ON p.paper_id = s.paper_id "
        "WHERE s.passed_filter = 1 "
        "AND p.paper_id NOT IN (SELECT paper_id FROM recommendations "
        "WHERE date >= date(?, '-30 days') AND date < ?)",
        (d, d),
    ).fetchall()
    scored = compute_scores(rows, weights, kw_config)
    scored.sort(key=lambda e: e["total_score"], reverse=True)
    top = scored[:top_n]
    conn.execute("DELETE FROM recommendations WHERE date = ?", (d,))
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [(d, e["paper_id"], e["total_score"], e["grade"]) for e in top],
    )
    conn.commit()
    return top


def main():
    ap = argparse.ArgumentParser(description="四维加权打分与每日 Top 推荐")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--top", type=int, default=15, help="Top N（默认 15）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    top = run(conn, load_weights(), load_kw_config(), top_n=args.top)
    conn.close()
    if not top:
        print("暂无符合条件的论文（passed_filter=1 且未被 AI 否决）")
        return
    print(f"Top {len(top)} 推荐：")
    for i, e in enumerate(top, 1):
        title = (e["title"] or "")[:60]
        print(f"{i:2d}. [{e['total_score']:5.2f}] {e['grade']:<10} {e['paper_id']}  {title}")


if __name__ == "__main__":
    main()
