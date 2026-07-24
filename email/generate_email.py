"""Phase 11：生成当日科研日报 HTML（可选 SMTP 发送）。

日报结构（模板 email/template.html）：
- 顶部统计条：昨日入库 / 通过过滤总数，以及通过论文按关键词组
  （物种/方法/工具/概念，组间可重叠）的计数；
- 卡片式推荐（Part 1/2 合并）：每篇一卡按等级配色（Must Read 红 /
  Important 橙 / Reference 灰），卡面为序号 + 等级徽标 + 标题 +
  期刊·日期 + DOI 链接 + 中文一句话（one_line_summary_cn），详细解读收进
  <details> 下拉（abstract_cn 中文压缩摘要、推荐理由、作者、一键反馈）。
  全卡片只显示中文，不出现英文摘要；
- 今日推荐文献价值总结：AI 基于全部推荐论文标题+一句话生成的趋势分析
  （prompt 在 prompts/daily_trend_prompt.txt；无推荐或 AI 失败时降级为静态文案）；
- 末尾"近 30 天趋势"：email/output/trend_summary.txt 存在且 mtime ≤ 7 天时附加
  （由 ranking/trends.py 每周一生成）。
缺 one_line_summary_cn 或 abstract_cn 的论文先调用 AI 补齐并写回 paper_scores
（复用 relevance prompt，AI 失败才降级）。反馈为指向本机 127.0.0.1:8710
小服务的链接（feedback/server.py），点击即写 feedback jsonl，不再弹邮件客户端。
不带 --send：写 email/output/YYYY-MM-DD.html；带 --send：smtplib 发送
（465→SSL，587→STARTTLS；SMTP 未配置时打印明确提示并以退出码 2 退出）。
收件人称呼取 .env 的 USER_NAME（默认"研究者"）。

注意：本目录（email/）不得放 __init__.py —— 本项目目录名与标准库 email 重名，
保持为无 __init__.py 的目录可让标准库 email（regular package）优先解析，
smtplib / email.mime 才能正常工作。
"""
import argparse
import html
import json
import smtplib
import sys
import time
from datetime import date as date_cls, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import load_config, match_keywords  # noqa: E402
from processing.paper_analyzer import build_prompt, dump_failure, load_profile_summary  # noqa: E402

GRADES = ("Must Read", "Important", "Reference")
GRADE_CSS = {"Must Read": "badge-must", "Important": "badge-important",
             "Reference": "badge-reference"}
CARD_CSS = {"Must Read": "card-must", "Important": "card-important",
            "Reference": "card-reference"}
RATINGS = (("good", "感兴趣"), ("ok", "一般"), ("bad", "不相关"))
FEEDBACK_BASE = "http://127.0.0.1:8710/feedback"
GROUP_NAMES = (("species", "物种"), ("methods", "方法"), ("tools", "工具"), ("concept", "概念"))
FALLBACK_SUMMARY = "今日暂无足够的评分数据生成趋势总结，以下为按综合评分排序的推荐文献。"
TREND_FILE = ROOT / "email" / "output" / "trend_summary.txt"
TREND_MAX_AGE = 7 * 86400  # 趋势总结超过 7 天不再展示


def one_line(row) -> str:
    """卡片上的一句话说明：中文一句话 → 推荐理由，逐级降级（不出现英文摘要）。"""
    if row["one_line_summary_cn"]:
        return row["one_line_summary_cn"]
    if row["reason"]:
        return row["reason"]
    return "（暂无中文总结）"


def summarize_trend(rows) -> str:
    """调用 AI 对全部推荐论文做趋势分析（150-250 字）；无推荐或 AI 失败时降级。"""
    if not rows:
        return FALLBACK_SUMMARY
    papers = "\n".join(f"- {r['title']}：{one_line(r)}" for r in rows)
    template = (ROOT / "prompts" / "daily_trend_prompt.txt").read_text(encoding="utf-8")
    prompt = template.replace("{{n}}", str(len(rows))).replace("{{papers}}", papers)
    try:
        return call_model(prompt, response_format="text").strip()
    except Exception:  # noqa: BLE001
        return FALLBACK_SUMMARY


def feedback_links(paper_id: str) -> str:
    """一键反馈链接：指向本机 8710 小服务，点击即写 jsonl。"""
    pid = quote(paper_id)
    return "".join(
        f'<a href="{FEEDBACK_BASE}?paper_id={pid}&rating={rating}">{label}</a>'
        for rating, label in RATINGS
    )


def render_card(rank: int, row) -> str:
    """渲染单篇论文卡片：卡面中文速览 + <details> 下拉详细解读（无英文摘要）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    doi_part = (f' · DOI: <a href="{esc(url)}">{esc(row["doi"])}</a>' if row["doi"] else "")
    badge = GRADE_CSS.get(row["grade"], "badge-reference")
    card = CARD_CSS.get(row["grade"], "card-reference")
    abstract_cn = row["abstract_cn"] or ""
    return f"""
<div class="card {card}">
  <h3>{rank}. {esc(row["title"])}<span class="badge {badge}">{esc(row["grade"])} {row["total_score"]}</span></h3>
  <div class="meta">{esc(row["journal"])} · {esc(row["date"])}{doi_part}</div>
  <p class="cn"><strong>中文一句话：</strong>{esc(one_line(row))}</p>
  <details><summary>详细解读</summary>
    {f'<p class="cn-abstract"><strong>中文摘要：</strong>{esc(abstract_cn)}</p>' if abstract_cn else ""}
    {f'<p class="reason"><strong>推荐理由：</strong>{esc(row["reason"])}</p>' if row["reason"] else ""}
    <div class="meta">作者：{esc(row["authors"])}</div>
    <div class="feedback">{feedback_links(row["paper_id"])}</div>
  </details>
</div>"""


def fetch_rows(date_str: str, conn) -> list:
    """取当日推荐论文（按 total_score 降序）及渲染所需字段。"""
    return conn.execute(
        "SELECT r.grade, r.total_score, p.paper_id, p.title, p.authors, p.journal, "
        "p.date, p.doi, p.url, p.abstract, s.reason, s.one_line_summary_cn, s.abstract_cn "
        "FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "LEFT JOIN paper_scores s ON r.paper_id = s.paper_id "
        "WHERE r.date = ? ORDER BY r.total_score DESC",
        (date_str,),
    ).fetchall()


def ensure_one_liners(conn, rows) -> None:
    """推荐论文中缺中文一句话或中文压缩摘要的，现场调用 AI 补齐（复用评分 prompt）。

    paper_analyzer 默认只评分 rule_score 前 20 篇，Top 推荐里可能混入未评分论文；
    老数据 abstract_cn 为 NULL。这里保证进日报的每篇都有"痛点+方法+发现"式中文
    一句话和 100-150 字中文压缩摘要，写回 paper_scores（已有值不覆盖）。
    AI 失败的保留降级文本。
    """
    missing = [r for r in rows if not r["one_line_summary_cn"] or not r["abstract_cn"]]
    if not missing:
        return
    template = (ROOT / "prompts" / "relevance_scoring_prompt.txt").read_text(encoding="utf-8")
    profile = load_profile_summary()
    for r in missing:
        prompt = build_prompt(template, profile, r["title"], r["abstract"])
        try:
            result = call_model(prompt, response_format="json")
        except Exception as e:  # noqa: BLE001
            dump_failure(r["paper_id"], e)
            print(f"[跳过] {r['paper_id']} 中文摘要生成失败：{e}")
            continue
        conn.execute(
            "UPDATE paper_scores SET ai_score=COALESCE(ai_score, ?), "
            "category=COALESCE(category, ?), reason=COALESCE(reason, ?), "
            "one_line_summary_cn=COALESCE(one_line_summary_cn, ?), "
            "abstract_cn=COALESCE(abstract_cn, ?), "
            "reproducibility=COALESCE(reproducibility, ?) "
            "WHERE paper_id=?",
            (float(result.get("relevance_score", 0)), result.get("category", ""),
             result.get("reason", ""), result.get("one_line_summary_cn", ""),
             result.get("abstract_cn", ""),
             json.dumps(result.get("reproducibility") or {}, ensure_ascii=False),
             r["paper_id"]),
        )
        conn.commit()
        print(f"[补齐] {r['paper_id']} 中文一句话/压缩摘要已生成")


def render_stats_bar(date_str: str, conn) -> str:
    """统计条：昨日入库 / 通过过滤总数 + 通过论文按关键词组（可重叠）计数。"""
    try:
        yesterday = (date_cls.fromisoformat(date_str) - timedelta(days=1)).isoformat()
    except ValueError:
        return ""
    total = conn.execute("SELECT COUNT(*) FROM papers WHERE date = ?",
                         (yesterday,)).fetchone()[0]
    rows = conn.execute(
        "SELECT p.title, p.abstract FROM papers p "
        "JOIN paper_scores s ON p.paper_id = s.paper_id "
        "WHERE p.date = ? AND s.passed_filter = 1",
        (yesterday,),
    ).fetchall()
    config = load_config()
    counts = {key: 0 for key, _ in GROUP_NAMES}
    for r in rows:
        groups = {h["group"] for h in match_keywords(r["title"], r["abstract"], config)["hits"]}
        for g in groups:
            if g in counts:
                counts[g] += 1
    dist = " · ".join(f"{name} <b>{counts[key]}</b>" for key, name in GROUP_NAMES)
    return (f'<div class="stats-bar">昨日（{yesterday}）入库 <b>{total}</b> 篇，'
            f'通过过滤 <b>{len(rows)}</b> 篇。通过论文关键词组分布：{dist}（组间可重叠）</div>')


def load_trend_block() -> str:
    """读取 ranking/trends.py 生成的近 30 天趋势总结（mtime ≤ 7 天才展示）。"""
    if not TREND_FILE.exists():
        return ""
    if time.time() - TREND_FILE.stat().st_mtime > TREND_MAX_AGE:
        return ""
    text = TREND_FILE.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    return ('<h2>近 30 天趋势</h2>\n'
            '<p class="layer">ranking/trends.py 每周一更新 · 近 30 天推荐论文的整体走向</p>\n'
            f'<div class="trend">{html.escape(text)}</div>')


def build_html(date_str: str, conn, mail_to: str, summary: str = None,
               user_name: str = "研究者") -> str:
    """按日期生成日报 HTML（summary 为 None 时自动调用 AI 生成当日趋势总结）。"""
    rows = fetch_rows(date_str, conn)
    ensure_one_liners(conn, rows)
    if any(not r["one_line_summary_cn"] or not r["abstract_cn"] for r in rows):
        rows = fetch_rows(date_str, conn)  # 重取，拿到补齐的内容
    if summary is None:
        summary = summarize_trend(rows)
    cards = ("\n".join(render_card(i, r) for i, r in enumerate(rows, 1))
             if rows else "<p>今日暂无推荐文献。</p>")
    template = (ROOT / "email" / "template.html").read_text(encoding="utf-8")
    return (template.replace("{{date}}", date_str)
                    .replace("{{user_name}}", html.escape(user_name))
                    .replace("{{count}}", str(len(rows)))
                    .replace("{{stats_bar}}", render_stats_bar(date_str, conn))
                    .replace("{{cards}}", cards)
                    .replace("{{trend_summary}}", html.escape(summary))
                    .replace("{{trend_block}}", load_trend_block()))


def send_email(html_body: str, subject: str, env: dict) -> None:
    """SMTP 发送（465→SSL，587→STARTTLS）；未配置时打印提示并以退出码 2 退出。"""
    host = (env.get("SMTP_HOST") or "").strip()
    if not host:
        print("SMTP 未配置：请在 .env 中设置 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/MAIL_TO")
        sys.exit(2)
    port = int((env.get("SMTP_PORT") or "465").strip())
    user = (env.get("SMTP_USER") or "").strip()
    password = (env.get("SMTP_PASSWORD") or "").strip()
    mail_to = (env.get("MAIL_TO") or "").strip()
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Research Radar", user))
    msg["To"] = mail_to
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, password)
            s.sendmail(user, [mail_to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(user, [mail_to], msg.as_string())
    print(f"日报已发送至 {mail_to}")


def main():
    ap = argparse.ArgumentParser(description="生成当日科研日报 HTML")
    ap.add_argument("--date", default=date_cls.today().isoformat(), help="YYYY-MM-DD（默认今天）")
    ap.add_argument("--send", action="store_true", help="生成后经 SMTP 发送")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    env = dotenv_values(ROOT / ".env")
    mail_to = (env.get("MAIL_TO") or "").strip()
    user_name = (env.get("USER_NAME") or "").strip() or "研究者"
    conn = get_conn(args.db)
    init_db(conn)
    html_body = build_html(args.date, conn, mail_to, user_name=user_name)
    conn.close()

    if not args.send:
        out = ROOT / "email" / "output" / f"{args.date}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(f"日报已生成：{out}")
        return
    send_email(html_body, f"Daily Literature Intelligence Report · {args.date}", env)


if __name__ == "__main__":
    main()
