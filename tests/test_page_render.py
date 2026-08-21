"""generate_page 网页版渲染冒烟测试（临时 DB，无网络/AI）。

注意：项目 email/ 目录与标准库 email 重名（无 __init__.py，标准库优先），
generate_page / generate_email 均须按文件路径加载。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location("generate_page", ROOT / "email" / "generate_page.py")
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)

TREND = {"overview": "测试趋势总览", "directions": ["方向一：测试"],
         "common_trend": "测试共同趋势", "value": "测试价值"}


def _seed_db(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", "Single-cell atlas of octopus brain", "We present ...",
          "Doe J, Smith A", "bioRxiv", "2026-07-22", "10.1", "https://www.biorxiv.org/content/10.1"),
         ("pubmed:10.2", "Brain evolution in annelids", "Here we show ...",
          "Lee B", "Nature", "2026-07-22", "10.2", "https://pubmed.ncbi.nlm.nih.gov/2/"),
         ("pubmed:10.3", "Organ evolution in flatworms", "Our data ...",
          "Wang C", "eLife", "2026-07-22", "10.3", "https://pubmed.ncbi.nlm.nih.gov/3/")],
    )
    # abstract_cn/title_cn 预先填好，避免测试触发真实 AI 调用
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, title_cn, "
        "one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", 50, 1, "核心方向高度相关", "章鱼脑单细胞图谱",
          "为解决章鱼脑细胞类型不清问题，作者用单细胞转录组构建全脑图谱，发现深度同源细胞类型。",
          "章鱼脑神经元类型缺乏系统图谱。作者用单细胞转录组测序构建全脑图谱，"
          "鉴定出数十种细胞类型，发现其与脊椎动物脑细胞存在深度同源，"
          "为理解复杂脑的独立演化提供细胞层面的证据。"),
         ("pubmed:10.2", 40, 1, "神经系统演化直接相关", "环节动物脑演化",
          "为解决两侧对称动物脑起源争议，作者比较环节动物脑转录组，发现保守神经发育模块。",
          "环节动物与脊椎动物脑的同源性长期存疑。作者比较多个环节动物物种的脑转录组，"
          "发现保守的神经发育调控模块，支持两侧对称动物脑的共同起源假说。"),
         ("pubmed:10.3", 20, 1, "器官演化相关", "扁形动物器官演化",
          "为解决扁形动物器官演化机制不清问题，作者做比较转录组分析，发现同源调控网络。",
          "扁形动物器官多样性机制不明。作者比较多个物种转录组，发现保守器官调控网络。")],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-23", "biorxiv:10.1", 7.2, "Must Read"),
         ("2026-07-23", "pubmed:10.2", 5.8, "Important"),
         ("2026-07-23", "pubmed:10.3", 3.4, "Relate")],
    )
    conn.commit()
    return conn


def test_page_overview_cards_details_and_feedback(tmp_path):
    """网页版：概览行 + 卡片 <details> 折叠详情 + JS fetch 反馈按钮（不跳转）。"""
    conn = _seed_db(tmp_path)
    html_body = gp.build_page_html("2026-07-23", conn, trend=TREND)
    conn.close()
    # 顶部概览行：篇数分等级统计 + 命中关键词数
    assert "今日推送 3 篇（Must Read 1 · Important 1 · Relate 1）" in html_body
    assert "命中关键词" in html_body
    # 卡片：Rank + 等级配色 + 标题链接 + 中文标题
    assert "Rank 1" in html_body and "Rank 3" in html_body
    assert "c-must" in html_body and "c-important" in html_body and "c-relate" in html_body
    assert "b-must" in html_body and "b-important" in html_body and "b-relate" in html_body
    assert 'href="https://www.biorxiv.org/content/10.1"' in html_body
    assert "章鱼脑单细胞图谱" in html_body
    # 详情折叠进 <details>：中英文摘要、推荐理由
    assert '<details class="card-details">' in html_body
    assert "为理解复杂脑的独立演化提供细胞层面的证据。" in html_body
    assert "We present" in html_body
    assert "核心方向高度相关" in html_body
    # 反馈按钮：data-url（& 转义为 &amp;），JS fetch 提交，无跳转链接
    assert ('data-url="http://127.0.0.1:8710/feedback'
            '?paper_id=biorxiv%3A10.1&amp;rating=good"') in html_body
    for rating in ("good", "bad", "read", "star"):
        assert f"rating={rating}" in html_body
    for label in ("相关", "不相关", "已读", "收藏"):
        assert label in html_body
    assert "mailto:" not in html_body
    # 底部价值总结（复用 trend 分块渲染）
    assert "今日推荐文献价值总结" in html_body
    assert "测试趋势总览" in html_body and "聚焦方向" in html_body


def test_page_has_keyword_submit_and_reason_ui(tmp_path):
    """网页版：文末关键词/文献提交区块 + 每卡隐藏的不相关原因行。"""
    conn = _seed_db(tmp_path)
    html_body = gp.build_page_html("2026-07-23", conn, trend=TREND)
    conn.close()
    # 两个提交区块与对应端点
    assert 'id="kw-submit"' in html_body and 'id="papers-submit"' in html_body
    assert "/keywords?text=" in html_body and "/keyword_papers?ids=" in html_body
    # 原因行：挂在每篇卡片内，默认隐藏，bad 按钮 URL 带 reason 参数
    assert html_body.count('class="reason-row"') == 3
    assert "不相关原因（可选）" in html_body
    assert "reason=" in html_body
    for reason in gp.REASON_PRESETS:
        assert reason in html_body
    assert 'class="reason-input"' in html_body and "reason-custom" in html_body


def test_write_page_and_index(tmp_path):
    """write_page 落盘 daily/ 页；write_index 生成跳转到最新日期的归档首页。"""
    docs = tmp_path / "docs"
    gp.write_page(docs, "2026-07-23", "<html>p1</html>")
    page = gp.write_page(docs, "2026-07-24", "<html>p2</html>")
    assert page.read_text(encoding="utf-8") == "<html>p2</html>"
    index = gp.write_index(docs)
    text = index.read_text(encoding="utf-8")
    assert 'content="0; url=daily/2026-07-24.html"' in text
    assert "daily/2026-07-23.html" in text
    # 新日期排在旧日期之前
    assert text.index("daily/2026-07-24.html") < text.rindex("daily/2026-07-23.html")
