"""Phase 11：生成当日科研日报 HTML（可选 SMTP 发送）。

日报为三段式结构（模板 email/template.html）：
- Part 1 · 今日论文新闻摘要：快速浏览层，按 total_score 排序的编号列表，
  每篇为等级徽标 + 标题 + 中文一句话（one_line_summary_cn，缺失时降级为
  推荐理由 → 摘要截断）+ 期刊 · 日期；
- Part 2 · 论文详情：完整卡片（作者/摘要/推荐理由/反馈按钮）；
- Part 3 · 今日推荐文献价值总结：AI 基于全部推荐论文标题+一句话生成的
  趋势分析（prompt 在 prompts/daily_trend_prompt.txt；无推荐或 AI 失败时
  降级为静态文案）。
反馈按钮为 mailto: 链接，主题携带 paper_id + rating。
不带 --send：写 email/output/YYYY-MM-DD.html；带 --send：smtplib 发送
（465→SSL，587→STARTTLS；SMTP 未配置时打印明确提示并以退出码 2 退出）。
收件人称呼取 .env 的 USER_NAME（默认"研究者"）。

注意：本目录（email/）不得放 __init__.py —— 本项目目录名与标准库 email 重名，
保持为无 __init__.py 的目录可让标准库 email（regular package）优先解析，
smtplib / email.mime 才能正常工作。
"""
import argparse
import html
import smtplib
import sys
from datetime import date as date_cls
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from database.db import get_conn, init_db  # noqa: E402

GRADES = ("Must Read", "Important", "Reference")
GRADE_CSS = {"Must Read": "badge-must", "Important": "badge-important",
             "Reference": "badge-reference"}
RATINGS = (("good", "符合需求"), ("ok", "一般符合"), ("bad", "不符合需求"))
FALLBACK_SUMMARY = "今日暂无足够的评分数据生成趋势总结，以下为按综合评分排序的推荐文献。"


def one_line(row) -> str:
    """Part 1 的一句话说明：中文一句话 → 推荐理由 → 摘要截断，逐级降级。"""
    if row["one_line_summary_cn"]:
        return row["one_line_summary_cn"]
    if row["reason"]:
        return row["reason"]
    return " ".join((row["abstract"] or "").split())[:80]


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


def render_quick_item(rank: int, row) -> str:
    """渲染 Part 1 的单条速览。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    css = GRADE_CSS.get(row["grade"], "badge-reference")
    return f"""
<div class="qitem">
  <span class="qnum">{rank}</span><span class="badge {css}">{esc(row["grade"])}</span>
  <div class="qtitle">{esc(row["title"])}</div>
  <div class="qline">{esc(one_line(row))}</div>
  <div class="qmeta">{esc(row["journal"])} · {esc(row["date"])}</div>
</div>"""


def render_card(rank: int, row, mail_to: str) -> str:
    """渲染 Part 2 的单篇论文详情卡片（含 mailto 反馈按钮）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    doi_part = (f' · DOI: <a href="{esc(url)}">{esc(row["doi"])}</a>' if row["doi"] else "")
    buttons = []
    for rating, label in RATINGS:
        subject = quote(f"[RR] paper_id={row['paper_id']} rating={rating}")
        buttons.append(f'<a href="mailto:{esc(mail_to)}?subject={subject}">{label}</a>')
    cn = row["one_line_summary_cn"]
    css = GRADE_CSS.get(row["grade"], "badge-reference")
    return f"""
<div class="card">
  <h3>{rank}. {esc(row["title"])}<span class="badge {css}">{esc(row["grade"])} {row["total_score"]}</span></h3>
  <div class="meta">{esc(row["authors"])} · {esc(row["journal"])} · {esc(row["date"])}{doi_part}</div>
  <p class="abstract">{esc(row["abstract"])}</p>
  {f'<p class="cn"><strong>中文一句话：</strong>{esc(cn)}</p>' if cn else ""}
  {f'<p class="reason"><strong>推荐理由：</strong>{esc(row["reason"])}</p>' if row["reason"] else ""}
  <div class="feedback">{''.join(buttons)}</div>
</div>"""


def build_html(date_str: str, conn, mail_to: str, summary: str = None,
               user_name: str = "研究者") -> str:
    """按日期生成三段式日报 HTML（summary 为 None 时自动调用 AI 生成趋势总结）。"""
    rows = conn.execute(
        "SELECT r.grade, r.total_score, p.paper_id, p.title, p.authors, p.journal, "
        "p.date, p.doi, p.url, p.abstract, s.reason, s.one_line_summary_cn "
        "FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "LEFT JOIN paper_scores s ON r.paper_id = s.paper_id "
        "WHERE r.date = ? ORDER BY r.total_score DESC",
        (date_str,),
    ).fetchall()
    if summary is None:
        summary = summarize_trend(rows)
    if rows:
        part1 = "\n".join(render_quick_item(i, r) for i, r in enumerate(rows, 1))
        part2 = "\n".join(render_card(i, r, mail_to) for i, r in enumerate(rows, 1))
    else:
        part1 = part2 = "<p>今日暂无推荐文献。</p>"
    template = (ROOT / "email" / "template.html").read_text(encoding="utf-8")
    return (template.replace("{{date}}", date_str)
                    .replace("{{user_name}}", html.escape(user_name))
                    .replace("{{count}}", str(len(rows)))
                    .replace("{{part1_items}}", part1)
                    .replace("{{part2_cards}}", part2)
                    .replace("{{trend_summary}}", html.escape(summary)))


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
