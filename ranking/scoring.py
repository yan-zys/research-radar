"""四维加权打分与每日 Top 推荐。

各维度归一化到 0-10 后按 config/scoring.yaml 权重加权（total_score 0-10）：
- research_relevance：rule_score 除以候选池最大值 ×10
- ai_semantic_relevance：ai_score（缺失按 5；已评且 <3 的论文为 AI 否决，仅作兜底层候选）
- journal_influence：内置期刊分档 dict
- trend_value：外部趋势信号（ranking/trend_signals.py：PubMed 近 30 天发文热度
  归一化 0-8 + 顶刊当期命中 +2；offline 模式用本地语料库文档频次）
每日 Top 15 按四层梯队兜底选满（run() 详见其 docstring），全部写入 recommendations，
不再因总分低剔除（grade：≥7 Must Read / ≥5 Important / 其余 Relate；
同日期先清空旧记录再写入）。打印各层选取数与 Top15 简表。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import match_keywords  # noqa: E402
from ranking.trend_signals import build_trend_ctx, heat_of, salient_terms, trend_values  # noqa: E402


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
    """加载 keyword_config.yaml（显著词提取复用其命中逻辑）。"""
    return yaml.safe_load((ROOT / "config" / "keyword_config.yaml").read_text(encoding="utf-8"))


def compute_scores(rows, weights: dict, kw_config: dict, ai_veto: float = 3.0,
                   include_vetoed: bool = False, trend_ctx: dict = None) -> list:
    """对候选论文计算四维得分与 total_score（rows 为 papers JOIN paper_scores）。

    AI 否决：ai_score 已评且低于 ai_veto（默认 3）的论文默认直接剔除，
    避免规则分高但 AI 判定无关的论文混入推荐；include_vetoed=True 时保留
    （供 run() 的 C/D 兜底层统一算分，由调用方自行分层排序）。
    trend_ctx（build_trend_ctx 返回的 {count_fn, tj_names}）提供外部趋势信号；
    缺省时 trend_value 全部按 0 计（离线纯计算场景）。
    """
    enriched = []
    for r in rows:
        if not include_vetoed and r["ai_score"] is not None and float(r["ai_score"]) < ai_veto:
            continue
        e = {
            "paper_id": r["paper_id"], "title": r["title"], "journal": r["journal"],
            "rule_score": r["rule_score"] or 0,
            "ai_score": r["ai_score"] if r["ai_score"] is not None else 5,
        }
        if trend_ctx is not None:
            terms = salient_terms(match_keywords(r["title"], r["abstract"], kw_config),
                                  r["title"])
            e["trend_heat"] = heat_of(terms, trend_ctx["count_fn"])
            e["is_tj"] = (r["journal"] or "").strip().lower() in trend_ctx["tj_names"]
        enriched.append(e)
    if trend_ctx is not None:
        tv = trend_values(
            {e["paper_id"]: e["trend_heat"] for e in enriched},
            {e["paper_id"]: e.get("is_tj", False) for e in enriched})
    else:
        tv = {}
    max_rule = max((e["rule_score"] for e in enriched), default=0) or 1
    for e in enriched:
        dims = {
            "research_relevance": e["rule_score"] / max_rule * 10,
            "ai_semantic_relevance": float(e["ai_score"]),
            "journal_influence": journal_score(e["journal"]),
            "trend_value": tv.get(e["paper_id"], 0.0),
        }
        e["total_score"] = round(sum(dims[k] * weights.get(k, 0) for k in dims), 2)
        e["grade"] = grade_of(e["total_score"])
    return enriched


def run(conn, weights: dict, kw_config: dict, run_date=None, top_n: int = 15,
        ai_veto: float = 3.0, pub_date=None, offline: bool = False) -> list:
    """对候选论文打分，按四层梯队兜底选满 Top N 写入 recommendations（同日期先清空）。

    梯队（逐层补足 top_n 为止，每层内部按 total_score 降序，分层规则见 layered()）：
    - A 层：关键词通道、未被 AI 否决、不在 30 天去重窗口内；
    - B 层：顶刊通道、已评 AI、未被否决、不在去重窗口内；
    - C 层：被 AI 否决的关键词/顶刊论文、不在去重窗口内；
    - D 层：放宽 30 天去重（按 A→C 同序，排除已入选）。
    总分仍由 compute_scores 统一计算（同一归一化池，含被否决论文）；每条结果带
    "tier" 字段（"A"|"B"|"C"|"D"）标记来源层。仅当库内论文总数不足 top_n 时
    才返回少于 top_n 篇。30 天去重：近 30 天内已进过 recommendations 的论文
    不参与 A/B/C 层候选。
    pub_date（YYYY-MM-DD）：限定候选论文的发表日期（papers.date），用于补算
    某一天发表文献的历史日报；默认 None 不过滤（日常流程靠爬虫 --since 限制）。
    offline=True 时趋势维度只用本地语料库频次（不访问 PubMed，测试/无网用）。
    """
    if run_date is None:
        d = date.today().isoformat()
    elif isinstance(run_date, str):
        d = run_date
    else:
        d = run_date.isoformat()

    trend_ctx = build_trend_ctx(conn, d, offline=offline)
    tj_names = trend_ctx["tj_names"]

    def fetch(where=None, params=()):
        sql = ("SELECT p.paper_id, p.title, p.abstract, p.journal, "
               "s.rule_score, s.ai_score, s.passed_filter "
               "FROM papers p JOIN paper_scores s ON p.paper_id = s.paper_id")
        if pub_date is not None:
            sql += " WHERE p.date = ?"
            params = (pub_date,) + tuple(params)
            if where:
                sql += " AND " + where
        elif where:
            sql += " WHERE " + where
        return conn.execute(sql, params).fetchall()

    def layered(rows) -> list:
        """统一算分后按 A→B→C 分层排序（层内 total_score 降序），每条带 tier 标记。

        分层规则（顶刊通道上线后收紧，非顶刊且未过关键词过滤的论文一律不推荐，
        杜绝医学噪声兜底混入）：
        - A 层：passed_filter=1、未被 AI 否决（关键词通道核心候选）；
        - B 层：顶刊清单内期刊、已评 AI（ai_score 非空）、未被否决；
        - C 层：被 AI 否决（ai_score<3）但属于关键词通过或顶刊清单的论文；
        - 其余（非顶刊 + 规则未命中）：不推荐。
        negative 排除词命中的论文（医学/临床噪声）在所有层都剔除——既是用户
        的降噪要求，也避免日报正文含大量 cancer/tumor/clinical 词汇被邮箱
        反垃圾系统拦截（SMTP 550）。
        """
        rows = [r for r in rows
                if not match_keywords(r["title"], r["abstract"], kw_config)["negative"]]
        crit = {}
        for r in rows:
            is_tj = (r["journal"] or "").strip().lower() in tj_names
            veto = r["ai_score"] is not None and float(r["ai_score"]) < ai_veto
            if veto:
                tier = "C" if (r["passed_filter"] == 1 or is_tj) else None
            elif r["passed_filter"] == 1:
                tier = "A"
            elif is_tj and r["ai_score"] is not None:
                tier = "B"
            else:
                tier = None
            if tier:
                crit[r["paper_id"]] = tier
        eligible = [r for r in rows if r["paper_id"] in crit]
        scored = compute_scores(eligible, weights, kw_config, ai_veto=ai_veto,
                                include_vetoed=True, trend_ctx=trend_ctx)
        by_tier = {"A": [], "B": [], "C": []}
        for e in scored:
            e["tier"] = crit[e["paper_id"]]
            by_tier[e["tier"]].append(e)
        out = []
        for t in ("A", "B", "C"):
            by_tier[t].sort(key=lambda e: e["total_score"], reverse=True)
            out.extend(by_tier[t])
        return out

    dedup = ("p.paper_id NOT IN (SELECT paper_id FROM recommendations "
             "WHERE date >= date(?, '-30 days') AND date < ?)")
    top = layered(fetch(dedup, (d, d)))[:top_n]
    if len(top) < top_n:  # D 层：放宽 30 天去重，按 A→C 同序补足
        chosen = {e["paper_id"] for e in top}
        for e in layered(fetch()):
            if len(top) >= top_n:
                break
            if e["paper_id"] in chosen:
                continue
            e["tier"] = "D"
            top.append(e)
            chosen.add(e["paper_id"])
    conn.execute("DELETE FROM recommendations WHERE date = ?", (d,))
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [(d, e["paper_id"], e["total_score"], e["grade"]) for e in top],
    )
    conn.commit()
    getattr(trend_ctx["count_fn"], "flush", lambda: None)()
    counts = {t: sum(1 for e in top if e["tier"] == t) for t in "ABCD"}
    print(f"梯队选取：A={counts['A']} B={counts['B']} C={counts['C']} "
          f"D={counts['D']}，共 {len(top)} 篇")
    return top


def main():
    ap = argparse.ArgumentParser(description="四维加权打分与每日 Top 推荐")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--top", type=int, default=15, help="Top N（默认 15）")
    ap.add_argument("--date", default=None,
                    help="推荐日期 YYYY-MM-DD（默认今天；补算历史日报用）")
    ap.add_argument("--pub-date", default=None,
                    help="限定候选论文发表日期 YYYY-MM-DD（默认不过滤；"
                         "补算某日发表文献的历史日报用）")
    ap.add_argument("--offline", action="store_true",
                    help="趋势维度只用本地语料库频次，不访问 PubMed（无网/调试用）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    top = run(conn, load_weights(), load_kw_config(), run_date=args.date,
              top_n=args.top, pub_date=args.pub_date, offline=args.offline)
    conn.close()
    if not top:
        print("暂无论文可推荐（papers 表为空）")
        return
    print(f"Top {len(top)} 推荐：")
    for i, e in enumerate(top, 1):
        title = (e["title"] or "")[:60]
        print(f"{i:2d}. [{e['total_score']:5.2f}] {e['grade']:<10} "
              f"{e['tier']}层  {e['paper_id']}  {title}")


if __name__ == "__main__":
    main()
