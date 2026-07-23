"""Phase 11：生成当日科研日报 HTML（可选 SMTP 发送）。

结构：顶部"今日趋势总述"（AI 总结 Must Read 共同趋势，50-100 字；
AI 失败或未评分时降级为静态文案），然后 Must Read → Important → Reference 分区。
反馈按钮为 mailto: 链接，主题携带 paper_id + rating。
不带 --send：写 email/output/YYYY-MM-DD.html；带 --send：smtplib 发送
（465→SSL，587→STARTTLS；SMTP 未配置时打印明确提示并以退出码 2 退出）。

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
RATINGS = (("good", "符合需求"), ("ok", "一般符合"), ("bad", "不符合需求"))
FALLBACK_SUMMARY = "今日暂无足够的评分数据生成趋势总述，以下为按综合评分排序的推荐文献。"


def summarize_trend(must_reads) -> str:
    """调用 AI 总结 Must Read 共同趋势（50-100 字）；无 Must Read 或 AI 失败时降级。"""
    if not must_reads:
        return FALLBACK_SUMMARY
    titles = "\n".join(f"- {r['title']}" for r in must_reads)
    prompt = ("以下是今日推荐的必读论文标题：\n" + titles +
              "\n请用一两句话（50-100字）概括这些论文共同反映的研究趋势，只输出总结文字。")
    try:
        return call_model(prompt, response_format="text").strip()
    except Exception:  # noqa: BLE001
        return FALLBACK_SUMMARY


def render_card(row, mail_to: str) -> str:
    """渲染单篇论文卡片（含 mailto 反馈按钮）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    doi_part = (f' · DOI: <a href="{esc(url)}">{esc(row["doi"])}</a>' if row["doi"] else "")
    buttons = []
    for rating, label in RATINGS:
        subject = quote(f"[RR] paper_id={row['paper_id']} rating={rating}")
        buttons.append(f'<a href="mailto:{esc(mail_to)}?subject={subject}">{label}</a>')
    cn = row["one_line_summary_cn"]
    return f"""
<div class="card">
  <h3>{esc(row["title"])}<span class="badge">{esc(row["grade"])} {row["total_score"]}</span></h3>
  <div class="meta">{esc(row["authors"])} · {esc(row["journal"])} · {esc(row["date"])}{doi_part}</div>
  <p class="abstract">{esc(row["abstract"])}</p>
  {f'<p class="cn"><strong>中文一句话：</strong>{esc(cn)}</p>' if cn else ""}
  {f'<p class="reason"><strong>推荐理由：</strong>{esc(row["reason"])}</p>' if row["reason"] else ""}
  <div class="feedback">{''.join(buttons)}</div>
</div>"""


def build_html(date_str: str, conn, mail_to: str, summary: str = None) -> str:
    """按日期生成日报 HTML（summary 为 None 时自动调用 AI 生成趋势总述）。"""
    rows = conn.execute(
        "SELECT r.grade, r.total_score, p.paper_id, p.title, p.authors, p.journal, "
        "p.date, p.doi, p.url, p.abstract, s.reason, s.one_line_summary_cn "
        "FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "LEFT JOIN paper_scores s ON r.paper_id = s.paper_id "
        "WHERE r.date = ? ORDER BY r.total_score DESC",
        (date_str,),
    ).fetchall()
    by_grade = {g: [r for r in rows if r["grade"] == g] for g in GRADES}
    if summary is None:
        summary = summarize_trend(by_grade["Must Read"])
    sections = []
    for g in GRADES:
        if not by_grade[g]:
            continue
        cards = "\n".join(render_card(r, mail_to) for r in by_grade[g])
        sections.append(f"<h2>{g}（{len(by_grade[g])} 篇）</h2>\n{cards}")
    if not sections:
        sections.append("<p>今日暂无推荐文献。</p>")
    template = (ROOT / "email" / "template.html").read_text(encoding="utf-8")
    return (template.replace("{{date}}", date_str)
                    .replace("{{summary}}", html.escape(summary))
                    .replace("{{sections}}", "\n".join(sections)))


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
    conn = get_conn(args.db)
    init_db(conn)
    html_body = build_html(args.date, conn, mail_to)
    conn.close()

    if not args.send:
        out = ROOT / "email" / "output" / f"{args.date}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_body, encoding="utf-8")
        print(f"日报已生成：{out}")
        return
    send_email(html_body, f"Research Radar 日报 {args.date}", env)


if __name__ == "__main__":
    main()
