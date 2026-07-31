"""近 30 天推荐论文趋势模块（每周一由 daily_run.sh 触发）。

聚合近 30 天 recommendations：
- 关键词组（species/methods/tools/concept）命中计数（按篇，组间可重叠）；
- grade 分布（Must Read / Important / Reference）；
- 高频命中关键词 Top 10。
聚合（collect_stats，纯函数）与 AI 调用（generate_summary）分开，便于测试。
AI 生成约 200 字中文趋势总结，写入 email/output/trend_summary.txt，
日报末尾在文件 mtime ≤ 7 天时附带展示。
"""
import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import load_config, match_keywords  # noqa: E402

OUT_FILE = ROOT / "email" / "output" / "trend_summary.txt"
GROUP_NAMES = {"core": "核心", "species": "物种", "methods": "方法", "tools": "工具",
               "concept": "概念", "topics": "主题方向"}
TOP_KEYWORDS = 10


def collect_stats(conn, days: int = 30, end_date: str = None) -> dict:
    """聚合近 days 天 recommendations：分组计数、grade 分布、高频命中关键词。"""
    end = end_date or date.today().isoformat()
    rows = conn.execute(
        "SELECT r.grade, p.title, p.abstract FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "WHERE r.date >= date(?, ?) AND r.date <= ?",
        (end, f"-{days} days", end),
    ).fetchall()
    config = load_config()
    grade_counts = Counter()
    group_counts = Counter()
    keyword_counts = Counter()
    for r in rows:
        grade_counts[r["grade"]] += 1
        hits = match_keywords(r["title"], r["abstract"], config)["hits"]
        for g in {h["group"] for h in hits}:
            group_counts[g] += 1
        for kw in {h["keyword"] for h in hits}:
            keyword_counts[kw] += 1
    return {
        "days": days, "end_date": end, "total": len(rows),
        "grade_counts": dict(grade_counts),
        "group_counts": {GROUP_NAMES.get(g, g): c for g, c in group_counts.items()},
        "top_keywords": keyword_counts.most_common(TOP_KEYWORDS),
    }


def build_summary_prompt(stats: dict) -> str:
    """把聚合统计转成给 AI 的趋势总结 prompt。"""
    grades = "，".join(f"{g} {c} 篇" for g, c in sorted(stats["grade_counts"].items())) or "无"
    groups = "，".join(f"{g} {c} 篇" for g, c in stats["group_counts"].items()) or "无"
    kws = "，".join(f"{kw}（{c}）" for kw, c in stats["top_keywords"]) or "无"
    return (
        "你是一位演化生物学与单细胞转录组学领域的资深科研顾问。以下是某研究者"
        f"近 {stats['days']} 天（截至 {stats['end_date']}）的每日论文推荐聚合统计：\n"
        f"- 推荐总数：{stats['total']} 篇\n"
        f"- 等级分布：{grades}\n"
        f"- 关键词组分布（组间可重叠）：{groups}\n"
        f"- 高频命中关键词：{kws}\n\n"
        "请用约 200 字中文总结这段时间文献推荐反映出的研究趋势：哪些方向/方法/物种"
        "正在升温，哪些值得关注后续进展。直接输出总结正文，不要标题、不要前后缀。"
    )


def generate_summary(stats: dict) -> str:
    """调用 AI 生成约 200 字中文趋势总结。"""
    return call_model(build_summary_prompt(stats), response_format="text").strip()


def run(conn, days: int = 30, summarize_fn=None, out_file: Path = None) -> dict:
    """聚合 + AI 总结 + 写文件，返回 stats。summarize_fn 可注入（测试用假函数）。"""
    stats = collect_stats(conn, days=days)
    if stats["total"] == 0:
        print(f"近 {days} 天无推荐记录，跳过趋势总结。")
        return stats
    summarize_fn = summarize_fn or generate_summary
    try:
        summary = summarize_fn(stats)
    except Exception as e:  # noqa: BLE001
        print(f"[失败] 趋势总结 AI 调用失败：{e}")
        return stats
    out = Path(out_file) if out_file else OUT_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(summary + "\n", encoding="utf-8")
    print(f"近 {days} 天趋势总结已写入 {out}")
    return stats


def main():
    ap = argparse.ArgumentParser(description="近 30 天推荐论文趋势总结")
    ap.add_argument("--days", type=int, default=30, help="回看天数（默认 30）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    run(conn, days=args.days)
    conn.close()


if __name__ == "__main__":
    main()
