"""生成周报/月报科研趋势摘要 HTML（可选 SMTP 发送）。

用法：--days 7 为周报（每周一由 daily_run.sh 触发），--days 30 为月报（每月 1 号触发）。
从 recommendations 表取近 N 天（含今天）的论文，同一 paper_id 只保留最高分记录，
按 total_score 去重后取 Top 15。三段式结构（模板 email/digest_template.html）：
- Part 1 · 本周/本月文献趋势总结：AI 基于 Top 15 生成结构化 JSON
  （prompts/digest_trend_prompt.txt：overview / common_trend / leads），
  渲染为大字号分块 HTML（总览 + 共同技术趋势 + 跟踪线索·一是二是三是）；
- Part 2 · 推荐分布统计：纯 Python 聚合（compute_digest_stats）——收录总数与
  定级分布（Must Read/Important/Relate）、期刊分层（顶刊/领域权威/其他，
  沿用 generate_email 的内置名单）、主要来源期刊 Top 5、高频关键词 Top 10；
- Part 3 · 重点论文清单：Top 15 速览（序号 + 等级徽标 + 总分 + 标题 +
  精炼一句话 + 期刊·日期），复用日报的 render_news_list。
聚合/选文逻辑（select_digest_papers / compute_digest_stats）与 AI 调用分离，便于测试。
主题行："每周科研趋势 · 日期范围" / "每月科研趋势 · 日期范围"。
不带 --send：写 email/output/digest-YYYY-MM-DD-Nd.html。
"""
import argparse
import html
import importlib.util
import sys
from collections import Counter
from datetime import date as date_cls, timedelta
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import load_config  # noqa: E402

# email/ 无 __init__.py（刻意，避免遮蔽标准库 email），按文件路径加载日报模块复用助手
_spec = importlib.util.spec_from_file_location(
    "generate_email", ROOT / "email" / "generate_email.py")
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

FALLBACK_OVERVIEW = "本周期暂无足够的推荐数据生成趋势总结，以下为按综合评分排序的重点文献。"


def period_meta(days: int) -> dict:
    """周报/月报的文案元数据。"""
    if days <= 7:
        return {"label": "本周", "unit": "周", "subject": "每周科研趋势",
                "leads_title": "下周跟踪线索"}
    return {"label": "本月", "unit": "月", "subject": "每月科研趋势",
            "leads_title": "下月跟踪线索"}


def select_digest_papers(conn, days: int, end_date: str = None, top_n: int = 15) -> list:
    """取近 N 天（含 end_date 当天）recommendations，同一 paper_id 只留最高分，取 Top N。

    纯查询/聚合，不调用 AI，便于测试。
    """
    end = end_date or date_cls.today().isoformat()
    start = (date_cls.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        "SELECT r.grade, r.total_score, r.date AS rec_date, p.paper_id, p.title, "
        "p.authors, p.journal, p.date, p.doi, p.url, p.abstract, s.reason, s.title_cn, "
        "s.one_line_summary_cn, s.abstract_cn "
        "FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "LEFT JOIN paper_scores s ON r.paper_id = s.paper_id "
        "WHERE r.date >= ? AND r.date <= ? "
        "ORDER BY r.total_score DESC",
        (start, end),
    ).fetchall()
    best = {}
    for r in rows:  # 已按 total_score 降序，首次出现即为该 paper_id 最高分
        if r["paper_id"] not in best:
            best[r["paper_id"]] = r
    deduped = sorted(best.values(), key=lambda r: r["total_score"], reverse=True)
    return deduped[:top_n]


def compute_digest_stats(rows, config) -> dict:
    """Part 2 统计聚合（纯 Python）：总数、定级分布、期刊分层、来源期刊 Top5、关键词 Top10。"""
    grade_dist = {g: sum(1 for r in rows if r["grade"] == g) for g in ge.GRADES}
    tier_dist = {"顶刊": 0, "领域权威": 0, "其他": 0}
    journal_counter, kw_counter = Counter(), Counter()
    for r in rows:
        tier_dist[ge.journal_tier(r["journal"])] += 1
        journal_counter[(r["journal"] or "（未知期刊）").strip() or "（未知期刊）"] += 1
        kw_counter.update(ge.paper_keywords(r, config))
    return {
        "total": len(rows),
        "grade_dist": grade_dist,
        "tier_dist": tier_dist,
        "top_journals": journal_counter.most_common(5),
        "top_keywords": kw_counter.most_common(10),
    }


def summarize_digest(rows, days: int) -> dict:
    """调用 AI 对 Top 论文生成结构化趋势总结（JSON dict）；失败返回降级文案。"""
    meta = period_meta(days)
    fallback = {"overview": FALLBACK_OVERVIEW, "common_trend": "", "leads": []}
    if not rows:
        return fallback
    papers = "\n".join(f"- {r['title']}：{ge.one_line(r)}" for r in rows)
    template = (ROOT / "prompts" / "digest_trend_prompt.txt").read_text(encoding="utf-8")
    prompt = (template.replace("{{period}}", meta["label"])
                      .replace("{{period_unit}}", meta["unit"])
                      .replace("{{n}}", str(len(rows)))
                      .replace("{{papers}}", papers))
    try:
        result = call_model(prompt, response_format="json")
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(result, dict) or not result.get("overview"):
        return fallback
    result.setdefault("leads", [])
    return result


def render_digest_trend(trend: dict, leads_title: str) -> str:
    """Part 1 趋势总结渲染：总览 / 共同技术趋势 / 跟踪线索（一是/二是/三是）分块。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    parts = [f'<div class="trend-overview">{esc(trend.get("overview"))}</div>']
    if str(trend.get("common_trend") or "").strip():
        parts.append('<div class="trend-block"><div class="trend-block-title">共同技术趋势</div>'
                     f'<div class="trend-text">{esc(trend["common_trend"])}</div></div>')
    leads = [l for l in (trend.get("leads") or []) if str(l).strip()]
    if leads:
        items = "\n".join(
            f'<div class="trend-dir"><span class="trend-dir-no">'
            f'{ge.CN_NUMERALS[i] if i < len(ge.CN_NUMERALS) else f"其{i+1}"}</span>{esc(l)}</div>'
            for i, l in enumerate(leads))
        parts.append(f'<div class="trend-block"><div class="trend-block-title">{esc(leads_title)}</div>'
                     f"{items}</div>")
    return "\n".join(parts)


def render_digest_stats(stats: dict) -> str:
    """Part 2 统计块渲染。"""
    esc = lambda s: html.escape(str(s))  # noqa: E731
    gd = stats["grade_dist"]
    grade_html = " · ".join(f"{g} <b>{gd[g]}</b>" for g in ge.GRADES)
    td = stats["tier_dist"]
    tier_html = " · ".join(f"{t} <b>{td[t]}</b>" for t in ("顶刊", "领域权威", "其他"))
    journals_html = " · ".join(f"{esc(j)} <b>{n}</b>" for j, n in stats["top_journals"]) \
        or "（暂无数据）"
    keywords_html = " · ".join(f"{esc(k)} <b>{n}</b>" for k, n in stats["top_keywords"]) \
        or "（暂无命中关键词）"
    parts = [
        f'<div class="stat-row"><span class="stat-label">收录论文</span>'
        f'共 <b>{stats["total"]}</b> 篇</div>',
        f'<div class="stat-row"><span class="stat-label">定级分布</span>{grade_html}</div>',
        f'<div class="stat-row"><span class="stat-label">期刊分层</span>{tier_html}</div>',
        f'<div class="stat-row"><span class="stat-label">主要来源期刊</span>{journals_html}</div>',
        f'<div class="stat-row"><span class="stat-label">高频关键词</span>{keywords_html}</div>',
    ]
    return '<div class="stats-bar">' + "\n".join(parts) + "</div>"


def build_html(conn, days: int, end_date: str = None, trend: dict = None,
               user_name: str = "研究者") -> tuple:
    """生成周报/月报 HTML，返回 (html, rows, date_range)。trend 为 None 时自动调用 AI。"""
    end = end_date or date_cls.today().isoformat()
    start = (date_cls.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
    date_range = f"{start} ~ {end}"
    meta = period_meta(days)
    rows = select_digest_papers(conn, days, end_date=end)
    ge.ensure_one_liners(conn, rows)
    if any(not r["one_line_summary_cn"] for r in rows):
        rows = select_digest_papers(conn, days, end_date=end)  # 重取，拿到补齐内容
    if trend is None:
        trend = summarize_digest(rows, days)
    config = load_config()
    stats = compute_digest_stats(rows, config)
    template = (ROOT / "email" / "digest_template.html").read_text(encoding="utf-8")
    body = (template.replace("{{period_label}}", meta["label"])
                    .replace("{{date_range}}", date_range)
                    .replace("{{user_name}}", html.escape(user_name))
                    .replace("{{count}}", str(len(rows)))
                    .replace("{{part1_trend}}", render_digest_trend(trend, meta["leads_title"]))
                    .replace("{{part2_stats}}", render_digest_stats(stats))
                    .replace("{{part3_list}}", ge.render_news_list(rows)))
    return body, rows, date_range


def main():
    ap = argparse.ArgumentParser(description="生成周报/月报科研趋势摘要 HTML")
    ap.add_argument("--days", type=int, choices=(7, 30), required=True,
                    help="7=周报，30=月报")
    ap.add_argument("--send", action="store_true", help="生成后经 SMTP 发送")
    ap.add_argument("--date", default=None, help="截止日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    user_name = (env.get("USER_NAME") or "").strip() or "研究者"
    conn = get_conn(args.db)
    init_db(conn)
    html_body, rows, date_range = build_html(conn, args.days, end_date=args.date,
                                             user_name=user_name)
    conn.close()
    meta = period_meta(args.days)

    if not args.send:
        end = args.date or date_cls.today().isoformat()
        out = ROOT / "email" / "output" / f"digest-{end}-{args.days}d.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(f"{meta['label']}报已生成（{len(rows)} 篇）：{out}")
        return
    ge.send_email(html_body, f"{meta['subject']} · {date_range}", env)
    print(f"{meta['label']}报（{date_range}）共 {len(rows)} 篇")


if __name__ == "__main__":
    main()
