"""生成当日科研日报 HTML（可选 SMTP 发送）。

日报为三段式结构（模板 email/template.html，口径均为"今日"）：
- Part 1 · 今日论文新闻摘要：当日 Top 推荐逐篇速览——序号 + 等级徽标
  （Must Read 红 / Important 橙 / Relate 灰）+ 总分数值 + 标题 + 精炼中文一句话
  + 期刊·日期；
- Part 2 · 论文详细信息卡片：全部推荐按评分排序，每卡含 Rank + 等级徽标 + 总分、
  英文标题、中文标题翻译（title_cn）、作者、期刊·日期、DOI/PubMed 链接、命中关键词、
  推荐理由、中文摘要（abstract_cn）、英文原文摘要（直接显示，灰色左边框引用样式，
  全模板禁用 <details>）、4 项一键反馈（相关/不相关/已读/收藏 → 127.0.0.1:8710）；
- Part 3 · 今日推荐文献价值总结：AI 基于今日 Must Read/Important 论文生成
  （无 Must Read/Important 时基于评分 Top 5），prompt 在 prompts/daily_trend_prompt.txt，
  输出 JSON {"overview","directions","common_trend","value"}，渲染为分块 HTML
  （总览 / 聚焦方向·一是二是三是 / 共同趋势 / 对研究者的价值），正文字号 ≥14px、
  line-height 1.7；无推荐或 AI 失败时降级为静态文案。
缺 one_line_summary_cn / abstract_cn / title_cn 任一的论文先调用 AI 补齐并写回
paper_scores（COALESCE，已有值不覆盖；AI 失败才降级）。
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
from processing.keyword_filter import load_config, match_keywords  # noqa: E402
from processing.paper_analyzer import build_prompt, dump_failure, load_profile_summary  # noqa: E402

GRADES = ("Must Read", "Important", "Relate")
GRADE_CSS = {"Must Read": "badge-must", "Important": "badge-important",
             "Relate": "badge-relate"}
CARD_CSS = {"Must Read": "card-must", "Important": "card-important",
            "Relate": "card-relate"}
RATINGS = (("good", "相关"), ("bad", "不相关"), ("read", "已读"), ("star", "收藏"))
FEEDBACK_BASE = "http://127.0.0.1:8710/feedback"
FALLBACK_SUMMARY = "今日暂无足够的评分数据生成趋势总结，以下为按综合评分排序的推荐文献。"
CN_NUMERALS = ("一是", "二是", "三是", "四是", "五是", "六是")

# 期刊分层内置名单（大小写不敏感）：顶刊按前缀匹配（含子刊），领域权威按包含匹配
TOP_JOURNALS = ("nature", "science", "cell")
FIELD_JOURNALS = (
    "genome research", "genome biology", "genome medicine", "nucleic acids research",
    "bioinformatics", "briefings in bioinformatics", "elife", "plos biology",
    "plos genetics", "plos computational biology", "cell reports", "current biology",
    "molecular biology and evolution", "genome biology and evolution", "gigascience",
    "mbio", "pnas",
)


def one_line(row) -> str:
    """清单/卡片上的一句话说明：中文一句话 → 推荐理由，逐级降级。"""
    if row["one_line_summary_cn"]:
        return row["one_line_summary_cn"]
    if row["reason"]:
        return row["reason"]
    return "（暂无中文总结）"


def journal_tier(journal: str) -> str:
    """期刊分层：顶刊（Nature/Science/Cell 前缀）/ 领域权威（内置名单包含）/ 其他。"""
    j = (journal or "").strip().lower()
    if any(j.startswith(t) for t in TOP_JOURNALS):
        return "顶刊"
    if any(t in j for t in FIELD_JOURNALS):
        return "领域权威"
    return "其他"


def key_rows(rows) -> list:
    """价值总结的输入论文：今日 Must Read/Important；没有则取评分 Top 5。"""
    key = [r for r in rows if r["grade"] in ("Must Read", "Important")]
    return key if key else rows[:5]


def summarize_trend(rows) -> dict:
    """调用 AI 对重点论文生成结构化价值总结（JSON dict）；失败返回降级文案。"""
    fallback = {"overview": FALLBACK_SUMMARY, "directions": [],
                "common_trend": "", "value": ""}
    selected = key_rows(rows)
    if not selected:
        return fallback
    papers = "\n".join(f"- {r['title']}：{one_line(r)}" for r in selected)
    template = (ROOT / "prompts" / "daily_trend_prompt.txt").read_text(encoding="utf-8")
    prompt = template.replace("{{n}}", str(len(selected))).replace("{{papers}}", papers)
    try:
        result = call_model(prompt, response_format="json")
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(result, dict) or not result.get("overview"):
        return fallback
    result.setdefault("directions", [])
    return result


def render_trend_blocks(trend: dict) -> str:
    """Part 3 价值总结渲染：总览 / 聚焦方向（一是/二是/三是）/ 共同趋势 / 价值 分块。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    parts = [f'<div class="trend-overview">{esc(trend.get("overview"))}</div>']
    directions = [d for d in (trend.get("directions") or []) if str(d).strip()]
    if directions:
        items = "\n".join(
            f'<div class="trend-dir"><span class="trend-dir-no">'
            f'{CN_NUMERALS[i] if i < len(CN_NUMERALS) else f"其{i+1}"}</span>{esc(d)}</div>'
            for i, d in enumerate(directions))
        parts.append('<div class="trend-block"><div class="trend-block-title">聚焦方向</div>'
                     f"{items}</div>")
    if str(trend.get("common_trend") or "").strip():
        parts.append('<div class="trend-block"><div class="trend-block-title">共同趋势</div>'
                     f'<div class="trend-text">{esc(trend["common_trend"])}</div></div>')
    if str(trend.get("value") or "").strip():
        parts.append('<div class="trend-block"><div class="trend-block-title">对研究者的价值</div>'
                     f'<div class="trend-text">{esc(trend["value"])}</div></div>')
    return "\n".join(parts)


def paper_keywords(row, config) -> list:
    """该论文 match_keywords 命中的关键词列表（保持命中顺序去重）。"""
    hits = match_keywords(row["title"], row["abstract"], config)["hits"]
    seen, kws = set(), []
    for h in hits:
        kw = str(h["keyword"])
        if kw not in seen:
            seen.add(kw)
            kws.append(kw)
    return kws


def feedback_links(paper_id: str) -> str:
    """一键反馈链接（4 项：相关/不相关/已读/收藏），指向本机 8710 小服务。"""
    pid = quote(paper_id)
    return "".join(
        f'<a href="{FEEDBACK_BASE}?paper_id={pid}&rating={rating}">{label}</a>'
        for rating, label in RATINGS
    )


def paper_links(row) -> str:
    """DOI 链接 + 原文链接（按 URL 判断 PubMed / bioRxiv 标签）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    links = []
    if row["doi"]:
        doi_url = f"https://doi.org/{row['doi']}"
        links.append(f'DOI: <a href="{esc(doi_url)}">{esc(row["doi"])}</a>')
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    if url:
        low = url.lower()
        label = "PubMed 链接" if "pubmed" in low else \
                ("bioRxiv 链接" if "biorxiv" in low else "原文链接")
        links.append(f'{label}: <a href="{esc(url)}">{esc(url)}</a>')
    return " · ".join(links)


def render_news_list(rows) -> str:
    """Part 1 今日论文新闻摘要：序号 + 等级徽标 + 总分 + 标题 + 一句话 + 期刊·日期。"""
    if not rows:
        return "<p>今日暂无推荐文献。</p>"
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    items = []
    for i, r in enumerate(rows, 1):
        badge = GRADE_CSS.get(r["grade"], "badge-relate")
        items.append(
            f'<div class="quick-item"><span class="quick-rank">{i}</span>'
            f'<span class="badge {badge}">{esc(r["grade"])}</span>'
            f'<span class="score">{r["total_score"]}</span>'
            f'<div class="quick-body"><div class="quick-title">{esc(r["title"])}</div>'
            f'<div class="quick-cn">{esc(one_line(r))}</div>'
            f'<div class="meta">{esc(r["journal"])} · {esc(r["date"])}</div></div></div>'
        )
    return "\n".join(items)


def render_card(rank: int, row, config) -> str:
    """Part 2 单篇论文详情卡片：英文摘要直接显示（引用样式，不用 <details>）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    badge = GRADE_CSS.get(row["grade"], "badge-relate")
    card = CARD_CSS.get(row["grade"], "card-relate")
    kws = paper_keywords(row, config)
    links_html = paper_links(row)
    return f"""
<div class="card {card}">
  <h3>Rank {rank} <span class="badge {badge}">{esc(row["grade"])}</span><span class="score">{row["total_score"]}</span></h3>
  <div class="title-en">{esc(row["title"])}</div>
  {f'<div class="title-cn">{esc(row["title_cn"])}</div>' if row["title_cn"] else ""}
  <div class="meta">{esc(row["authors"])}</div>
  <div class="meta">{esc(row["journal"])} · {esc(row["date"])}{" · " + links_html if links_html else ""}</div>
  {f'<div class="meta keywords">Keywords: {esc(", ".join(kws))}</div>' if kws else ""}
  {f'<p class="reason"><strong>推荐理由：</strong>{esc(row["reason"])}</p>' if row["reason"] else ""}
  {f'<p class="cn-abstract"><strong>中文摘要：</strong>{esc(row["abstract_cn"])}</p>' if row["abstract_cn"] else ""}
  {f'<p class="en-abstract"><strong>英文摘要（原文）：</strong>{esc(row["abstract"])}</p>' if row["abstract"] else ""}
  <div class="feedback">{feedback_links(row["paper_id"])}</div>
</div>"""


def fetch_rows(date_str: str, conn) -> list:
    """取当日推荐论文（按 total_score 降序）及渲染所需字段。"""
    return conn.execute(
        "SELECT r.grade, r.total_score, p.paper_id, p.title, p.authors, p.journal, "
        "p.date, p.doi, p.url, p.abstract, s.reason, s.title_cn, "
        "s.one_line_summary_cn, s.abstract_cn "
        "FROM recommendations r "
        "JOIN papers p ON r.paper_id = p.paper_id "
        "LEFT JOIN paper_scores s ON r.paper_id = s.paper_id "
        "WHERE r.date = ? ORDER BY r.total_score DESC",
        (date_str,),
    ).fetchall()


def ensure_one_liners(conn, rows) -> None:
    """推荐论文中缺中文一句话、中文压缩摘要或中文标题翻译的，现场调用 AI 补齐。

    paper_analyzer 默认只评分 rule_score 前 20 篇，Top 推荐里可能混入未评分论文；
    老数据 abstract_cn/title_cn 为 NULL。这里保证进日报的每篇都有精炼中文一句话、
    中文压缩摘要和中文标题翻译，写回 paper_scores（COALESCE，已有值不覆盖）。
    AI 失败的保留降级文本。
    """
    missing = [r for r in rows
               if not r["one_line_summary_cn"] or not r["abstract_cn"] or not r["title_cn"]]
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
            print(f"[跳过] {r['paper_id']} 中文内容生成失败：{e}")
            continue
        conn.execute(
            "UPDATE paper_scores SET ai_score=COALESCE(ai_score, ?), "
            "category=COALESCE(category, ?), reason=COALESCE(reason, ?), "
            "title_cn=COALESCE(title_cn, ?), "
            "one_line_summary_cn=COALESCE(one_line_summary_cn, ?), "
            "abstract_cn=COALESCE(abstract_cn, ?), "
            "reproducibility=COALESCE(reproducibility, ?) "
            "WHERE paper_id=?",
            (float(result.get("relevance_score", 0)), result.get("category", ""),
             result.get("reason", ""), result.get("title_cn", ""),
             result.get("one_line_summary_cn", ""),
             result.get("abstract_cn", ""),
             json.dumps(result.get("reproducibility") or {}, ensure_ascii=False),
             r["paper_id"]),
        )
        conn.commit()
        print(f"[补齐] {r['paper_id']} 中文标题/一句话/压缩摘要已生成")


def build_html(date_str: str, conn, mail_to: str, trend: dict = None,
               user_name: str = "研究者") -> str:
    """按日期生成日报 HTML（trend 为 None 时自动调用 AI 生成 Part 3 价值总结）。"""
    rows = fetch_rows(date_str, conn)
    ensure_one_liners(conn, rows)
    if any(not r["one_line_summary_cn"] or not r["abstract_cn"] or not r["title_cn"]
           for r in rows):
        rows = fetch_rows(date_str, conn)  # 重取，拿到补齐的内容
    if trend is None:
        trend = summarize_trend(rows)
    config = load_config()
    cards = ("\n".join(render_card(i, r, config) for i, r in enumerate(rows, 1))
             if rows else "<p>今日暂无推荐文献。</p>")
    template = (ROOT / "email" / "template.html").read_text(encoding="utf-8")
    return (template.replace("{{date}}", date_str)
                    .replace("{{user_name}}", html.escape(user_name))
                    .replace("{{count}}", str(len(rows)))
                    .replace("{{part1_list}}", render_news_list(rows))
                    .replace("{{part2_cards}}", cards)
                    .replace("{{part3_trend}}", render_trend_blocks(trend)))


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
    print(f"邮件已发送至 {mail_to}")


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
