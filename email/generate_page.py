"""生成当日日报网页版（GitHub Pages 静态页，docs/daily/YYYY-MM-DD.html）。

页面承载原邮件 Part 2/3（模板 email/page_template.html，纯自包含、无外部依赖）：
- 顶部概览行：今日推送 N 篇（Must Read x · Important y · Relate z）· 命中关键词 M 个；
- 论文卡片流（灰底卡片，等级左色条沿用邮件配色）：可见区为 Rank + 等级徽标 + 总分、
  英文标题（可点链接）、中文标题、一句话、期刊·日期；<details> 折叠作者、DOI/原文
  链接、命中关键词、推荐理由、中文摘要、英文原文摘要；卡底 4 个反馈按钮
  （相关/不相关/已读/收藏）以 JS fetch 提交本机 8710 反馈服务（feedback/server.py，
  已带 CORS/PNA 头），成功原地高亮"已记录"、失败提示启动服务，全程不跳转页面；
- 底部今日推荐文献价值总结（复用 generate_email.summarize_trend/render_trend_blocks）。
同一次运行还会重写 docs/index.html：meta refresh 跳到最新日期页 + 全部日期链接列表。
无当日推荐也正常生成页面（显示"今日暂无推荐"），不做节假日特判。

注意：本目录（email/）不得放 __init__.py —— 目录名与标准库 email 重名，
保持为无 __init__.py 的目录可让标准库 email 优先解析（smtplib 才能正常工作），
因此 generate_email 按文件路径加载（与 generate_digest.py 同一模式）。
"""
import argparse
import html
import importlib.util
from datetime import date as date_cls
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("generate_email", ROOT / "email" / "generate_email.py")
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

CARD_CLASS = {"Must Read": "c-must", "Important": "c-important", "Relate": "c-relate"}
BADGE_CLASS = {"Must Read": "b-must", "Important": "b-important", "Relate": "b-relate"}
# “不相关”反馈后可点选的原因（落到反馈 jsonl 的 reason 字段，供后续学习）
REASON_PRESETS = ["方向不符", "物种不符", "医学·临床噪声", "方法不相关"]


def render_page_card(rank: int, row, config, kws: list) -> str:
    """单篇论文网页卡片：可见区 + <details> 折叠详情 + JS 反馈按钮（不跳转）。"""
    esc = lambda s: html.escape(str(s or ""))  # noqa: E731
    badge = BADGE_CLASS.get(row["grade"], "b-relate")
    card = CARD_CLASS.get(row["grade"], "c-relate")
    links_html = ge.paper_links(row)
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    title_en = esc(row["title"])
    if url:
        title_en = f'<a href="{esc(url)}" target="_blank" rel="noopener">{title_en}</a>'
    details = []
    if row["authors"]:
        details.append(f'<div class="d-block"><span class="lbl">作者：</span>{esc(row["authors"])}</div>')
    if links_html:
        details.append(f'<div class="d-block meta">{links_html}</div>')
    if kws:
        details.append(f'<div class="d-block keywords">命中关键词：{esc(", ".join(kws))}</div>')
    if row["reason"]:
        details.append(f'<div class="d-block"><span class="lbl">推荐理由：</span>{esc(row["reason"])}</div>')
    if row["abstract_cn"]:
        details.append(f'<div class="d-block"><span class="lbl">中文摘要：</span>{esc(row["abstract_cn"])}</div>')
    if row["abstract"]:
        details.append(f'<div class="en-abstract"><span class="lbl">英文摘要（原文）：</span>{esc(row["abstract"])}</div>')
    buttons = []
    for rating, label in ge.RATINGS:
        fb_url = esc(f'{ge.FEEDBACK_BASE}?paper_id={quote(row["paper_id"])}&rating={rating}')
        buttons.append(f'<button class="fb-btn" data-url="{fb_url}">{label}</button>')
    reason_base = esc(f'{ge.FEEDBACK_BASE}?paper_id={quote(row["paper_id"])}&rating=bad&reason=')
    reason_chips = " ".join(
        f'<button class="reason-btn" data-url="{reason_base}{quote(r)}">{r}</button>'
        for r in REASON_PRESETS
    )
    reason_row = (
        '<div class="reason-row"><span>不相关原因（可选）：</span>'
        f'{reason_chips}'
        '<input class="reason-input" placeholder="其他原因…">'
        f'<button class="reason-btn reason-custom" data-base="{reason_base}">提交</button>'
        '<span class="reason-status fb-status"></span></div>'
    )
    return f"""
<div class="card {card}">
  <div class="card-head"><span class="rank">Rank {rank}</span><span class="badge {badge}">{esc(row["grade"])}</span><span class="score">{row["total_score"]}</span></div>
  <div class="title-en">{title_en}</div>
  {f'<div class="title-cn">{esc(row["title_cn"])}</div>' if row["title_cn"] else ""}
  <div class="one-line">{esc(ge.one_line(row))}</div>
  <div class="meta">{esc(row["journal"])} · {esc(row["date"])}</div>
  <details class="card-details">
    <summary>详情（作者 / 链接 / 命中关键词 / 推荐理由 / 中英文摘要）</summary>
    {''.join(details)}
  </details>
  <div class="fb-row">{''.join(buttons)}<span class="fb-status"></span></div>
  {reason_row}
</div>"""


def build_page_html(date_str: str, conn, trend: dict = None) -> str:
    """按日期生成日报网页版 HTML；trend 为 None 时自动调用 AI 生成价值总结。"""
    rows = ge.fetch_rows(date_str, conn)
    ge.ensure_one_liners(conn, rows)
    if any(not r["one_line_summary_cn"] or not r["abstract_cn"] or not r["title_cn"]
           for r in rows):
        rows = ge.fetch_rows(date_str, conn)  # 重取，拿到补齐的内容
    if trend is None:
        trend = ge.summarize_trend(rows)
    config = ge.load_config()
    counts = {g: sum(1 for r in rows if r["grade"] == g) for g in ge.GRADES}
    all_kws, seen = [], set()
    card_rows = []
    for r in rows:
        kws = ge.paper_keywords(r, config)
        card_rows.append((r, kws))
        for kw in kws:
            if kw not in seen:
                seen.add(kw)
                all_kws.append(kw)
    if rows:
        overview_line = (f"今日推送 {len(rows)} 篇"
                         f"（Must Read {counts['Must Read']} · Important {counts['Important']}"
                         f" · Relate {counts['Relate']}）· 命中关键词 {len(all_kws)} 个")
    else:
        overview_line = "今日暂无推荐文献。"
    cards = ("\n".join(render_page_card(i, r, config, kws)
                       for i, (r, kws) in enumerate(card_rows, 1))
             if rows else "<p>今日暂无推荐文献。</p>")
    template = (ROOT / "email" / "page_template.html").read_text(encoding="utf-8")
    return (template.replace("{{date}}", date_str)
                    .replace("{{overview_line}}", html.escape(overview_line))
                    .replace("{{cards}}", cards)
                    .replace("{{trend}}", ge.render_trend_blocks(trend)))


def write_page(docs_dir: Path, date_str: str, html_body: str) -> Path:
    """写入 docs/daily/YYYY-MM-DD.html，返回路径。"""
    out = docs_dir / "daily" / f"{date_str}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_body, encoding="utf-8")
    return out


def write_index(docs_dir: Path) -> Path:
    """重写 docs/index.html：meta refresh 跳最新日期页 + 全部日期链接列表（倒序）。"""
    daily = docs_dir / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    dates = sorted(p.name[:-5] for p in daily.glob("*.html"))
    latest = dates[-1] if dates else None
    refresh = (f'<meta http-equiv="refresh" content="0; url=daily/{latest}.html">'
               if latest else "")
    items = "\n".join(f'<li><a href="daily/{d}.html">{d}</a></li>'
                      for d in reversed(dates))
    body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
{refresh}
<title>Research Radar · 日报归档</title>
<style>
  body {{ font-family: -apple-system, "Helvetica Neue", Arial, "PingFang SC", sans-serif;
         max-width: 640px; margin: 0 auto; padding: 40px 16px; color: #222; }}
  h1 {{ font-size: 20px; }}
  a {{ color: #0b5cad; text-decoration: none; }}
  li {{ margin: 6px 0; }}
</style>
</head>
<body>
<h1>Research Radar · 日报归档</h1>
<p>{f'最新日报：<a href="daily/{latest}.html">{latest}</a>（正在跳转…）' if latest else '暂无日报页面。'}</p>
<ul>
{items}
</ul>
</body>
</html>
"""
    out = docs_dir / "index.html"
    out.write_text(body, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description="生成当日日报网页版（docs/daily/YYYY-MM-DD.html）")
    ap.add_argument("--date", default=date_cls.today().isoformat(), help="YYYY-MM-DD（默认今天）")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--out", default=str(ROOT / "docs"), help="输出目录（默认 docs/）")
    args = ap.parse_args()

    conn = ge.get_conn(args.db)
    ge.init_db(conn)
    html_body = build_page_html(args.date, conn)
    conn.close()

    docs_dir = Path(args.out)
    page = write_page(docs_dir, args.date, html_body)
    index = write_index(docs_dir)
    print(f"日报网页版已生成：{page}")
    print(f"归档首页已更新：{index}")


if __name__ == "__main__":
    main()
