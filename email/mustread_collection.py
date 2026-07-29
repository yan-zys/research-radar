"""年度 Must Read 文献合集：全库四维评分（含在线趋势信号），总分 ≥7 的论文
按日报版式生成 HTML 邮件（可选 SMTP 发送）。

与日报的差异：
- 候选池 = 全库 A/B/C 口径（negative 排除词剔除；passed_filter=1 或
  顶刊清单内且已评 AI），评分归一化在该全库池内进行（日报是按当日池归一化，
  两者口径不同，合集分数与日报分数不可直接比较）；
- 只收录 Must Read（total_score ≥ 7），不凑数、不降档；
- 不写 recommendations 表（避免污染 30 天去重窗口）；
- 模板标题/问候语改为合集口径，正文结构与日报完全一致
  （Part 1 速览 → Part 2 详情卡片 → Part 3 价值总结）。

用法：
  venv/bin/python email/mustread_collection.py            # 只评分+打印清单+写 HTML
  venv/bin/python email/mustread_collection.py --send     # 并通过 SMTP 发送
  venv/bin/python email/mustread_collection.py --offline  # 趋势用本地语料库（调试用）

合并库：若 database/backfill_2026.db 存在（2026 年历史回填库），自动 ATTACH 合并
评分（主库优先去重）；--no-backfill 可关闭。只收录发表年份等于 --year 的论文。
"""
import argparse
import html
import sys
from datetime import date as date_cls
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "email"))

from dotenv import dotenv_values  # noqa: E402

from database.db import get_conn, init_db  # noqa: E402
from generate_email import (ensure_one_liners, render_card, render_news_list,  # noqa: E402
                            render_trend_blocks, send_email, summarize_trend)
from processing.keyword_filter import load_config, match_keywords  # noqa: E402
from ranking.scoring import compute_scores, load_kw_config, load_weights  # noqa: E402
from ranking.trend_signals import build_trend_ctx  # noqa: E402

MUST_READ_THRESHOLD = 7.0
BACKFILL_DB = ROOT / "database" / "backfill_2026.db"

POOL_SQL = (
    "SELECT p.paper_id, p.title, p.abstract, p.journal, p.date, "
    "s.rule_score, s.ai_score, s.passed_filter "
    "FROM papers p JOIN paper_scores s ON p.paper_id = s.paper_id"
)
RENDER_SQL = (
    "SELECT p.paper_id, p.title, p.authors, p.journal, p.date, p.doi, p.url, "
    "p.abstract, s.reason, s.title_cn, s.one_line_summary_cn, s.abstract_cn "
    "FROM papers p LEFT JOIN paper_scores s ON p.paper_id = s.paper_id "
    "WHERE p.paper_id = ?"
)


def eligible_pool(conns, kw_config, tj_names, year: str = None):
    """多库 A/B/C 口径候选（主库优先去重）：排除 negative 词；
    passed_filter=1 或（顶刊且已评 AI）；指定 year 时只收该年发表的论文。
    返回 (pool, conn_of)：pool 为 dict 行列表，conn_of 记录每篇来自哪个连接。"""
    pool, conn_of = [], {}
    for conn in conns:
        for r in conn.execute(POOL_SQL).fetchall():
            pid = r["paper_id"]
            if pid in conn_of:
                continue
            if year and r["date"] and not str(r["date"]).startswith(year):
                continue
            if match_keywords(r["title"], r["abstract"], kw_config)["negative"]:
                continue
            is_tj = (r["journal"] or "").strip().lower() in tj_names
            if r["passed_filter"] == 1 or (is_tj and r["ai_score"] is not None):
                pool.append(dict(r))
                conn_of[pid] = conn
    return pool, conn_of


def fetch_render_rows(conns, scored) -> list:
    """按评分结果取渲染所需全部字段（dict 行，附带 grade/total_score），多库依序查找。"""
    out = []
    for e in scored:
        r = None
        for conn in conns:
            r = conn.execute(RENDER_SQL, (e["paper_id"],)).fetchone()
            if r:
                break
        d = dict(r)
        d["grade"] = e["grade"]
        d["total_score"] = e["total_score"]
        out.append(d)
    return out


def ensure_all_one_liners(conn_of, rows) -> None:
    """按论文所属库分组调用 ensure_one_liners（UPDATE 须路由到正确的库）。"""
    groups = {}
    for r in rows:
        groups.setdefault(id(conn_of[r["paper_id"]]), []).append(r)
    for subset in groups.values():
        ensure_one_liners(conn_of[subset[0]["paper_id"]], subset)


def build_collection_html(conns, conn_of, rows, year: str, pool_size: int,
                          user_name: str, trend: dict = None) -> str:
    """套用日报模板，替换标题/问候/分层说明为合集口径（trend 为 None 时调 AI 生成）。"""
    ensure_all_one_liners(conn_of, rows)
    if any(not r["one_line_summary_cn"] or not r["abstract_cn"] or not r["title_cn"]
           for r in rows):
        ids = [r["paper_id"] for r in rows]
        scored = [{"paper_id": r["paper_id"], "grade": r["grade"],
                   "total_score": r["total_score"]} for r in rows]
        rows = fetch_render_rows(conns, scored)  # 重取，拿到补齐的中文内容
        assert [r["paper_id"] for r in rows] == ids
    if trend is None:
        trend = summarize_trend(rows)
    config = load_config()
    cards = "\n".join(render_card(i, r, config) for i, r in enumerate(rows, 1))
    template = (ROOT / "email" / "template.html").read_text(encoding="utf-8")
    body = (template.replace("{{date}}", year)
                    .replace("{{user_name}}", html.escape(user_name))
                    .replace("{{count}}", str(len(rows)))
                    .replace("{{part1_list}}", render_news_list(rows))
                    .replace("{{part2_cards}}", cards)
                    .replace("{{part3_trend}}", render_trend_blocks(trend)))
    body = body.replace(
        f"Daily Literature Intelligence Report · {year}",
        f"{year} 年度 Must Read 文献合集")
    body = body.replace(
        "今日为你筛选出",
        f"按现行四维评分标准（总分 ≥ {MUST_READ_THRESHOLD:g}）"
        f"从全库 {pool_size} 篇候选中评选出")
    body = body.replace("日报分三部分", "本合集分三部分")
    body = body.replace("Part 1 · 今日论文新闻摘要", "Part 1 · Must Read 论文新闻摘要")
    body = body.replace("快速浏览层 · 今日推荐逐篇速览", "快速浏览层 · 年度 Must Read 逐篇速览")
    body = body.replace("Part 3 · 今日推荐文献价值总结", "Part 3 · 年度 Must Read 文献价值总结")
    body = body.replace("趋势分析层 · 基于今日 Must Read/Important 论文 AI 生成（无则取评分 Top 5）",
                        "趋势分析层 · 基于年度 Must Read 论文 AI 生成")
    return body


def main():
    ap = argparse.ArgumentParser(description="年度 Must Read 文献合集")
    ap.add_argument("--year", default=str(date_cls.today().year), help="合集年份标签（默认今年）")
    ap.add_argument("--send", action="store_true", help="生成后经 SMTP 发送")
    ap.add_argument("--offline", action="store_true", help="趋势维度只用本地语料库（不访问 PubMed）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--no-backfill", action="store_true",
                    help="不合并 database/backfill_2026.db 回填库")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    conns = [conn]
    if not args.no_backfill and BACKFILL_DB.exists() and (
            args.db is None or Path(args.db).resolve() != BACKFILL_DB.resolve()):
        bf_conn = get_conn(str(BACKFILL_DB))
        init_db(bf_conn)
        conns.append(bf_conn)
        print(f"已合并回填库：{BACKFILL_DB}")
    weights, kw_config = load_weights(), load_kw_config()
    trend_ctx = build_trend_ctx(conn, date_cls.today().isoformat(), offline=args.offline)
    pool, conn_of = eligible_pool(conns, kw_config, trend_ctx["tj_names"], year=args.year)
    print(f"全库候选池（{args.year} 年）：{len(pool)} 篇（已排除 negative 词论文）")
    scored = compute_scores(pool, weights, kw_config, include_vetoed=True, trend_ctx=trend_ctx)
    getattr(trend_ctx["count_fn"], "flush", lambda: None)()
    scored.sort(key=lambda e: e["total_score"], reverse=True)

    print("=== 全库 Top 15 ===")
    for e in scored[:15]:
        print(f"{e['total_score']:5.2f} {e['grade']:<10} {e['journal'][:30]:<30} {e['title'][:60]}")
    mr = [e for e in scored if e["total_score"] >= MUST_READ_THRESHOLD]
    print(f"Must Read（≥{MUST_READ_THRESHOLD:g}）：{len(mr)} 篇")
    if not mr:
        print("按现行评分标准无 Must Read 论文，未生成邮件。")
        for c in conns:
            c.close()
        return

    env = dotenv_values(ROOT / ".env")
    user_name = (env.get("USER_NAME") or "").strip() or "研究者"
    rows = fetch_render_rows(conns, mr)
    body = build_collection_html(conns, conn_of, rows, args.year, len(pool), user_name)
    for c in conns:
        c.close()

    out = ROOT / "email" / "output" / f"{args.year}-mustread.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"合集 HTML 已生成：{out}")
    if args.send:
        send_email(body, f"{args.year} 年度 Must Read 文献合集 · {len(mr)} 篇", env)


if __name__ == "__main__":
    main()
