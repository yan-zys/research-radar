"""generate_email 渲染冒烟测试（临时 DB，无网络/AI）。

注意：项目 email/ 目录与标准库 email 重名（无 __init__.py，标准库优先），
因此不能用 `from email.generate_email import ...`，须按文件路径加载模块。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location("generate_email", ROOT / "email" / "generate_email.py")
ge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ge)

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


def test_three_part_structure_and_feedback_links(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com", trend=TREND)
    conn.close()
    # 三段式结构齐全
    assert "Part 1 · 今日论文新闻摘要" in html_body
    assert "Part 2 · 论文详细信息卡片" in html_body
    assert "Part 3 · 今日推荐文献价值总结" in html_body
    assert "测试趋势总览" in html_body
    assert "Single-cell atlas of octopus brain" in html_body
    assert "Brain evolution in annelids" in html_body
    # 中文标题翻译（title_cn）
    assert "章鱼脑单细胞图谱" in html_body
    # 一键反馈 4 项链接指向本机小服务，不再是 mailto
    assert "mailto:" not in html_body
    assert "http://127.0.0.1:8710/feedback?paper_id=biorxiv%3A10.1&rating=good" in html_body
    for rating in ("good", "bad", "read", "star"):
        assert f"rating={rating}" in html_body
    for label in ("相关", "不相关", "已读", "收藏"):
        assert label in html_body
    # 等级：Must Read / Important / Relate（不再有 Reference）
    assert "Must Read" in html_body and "Important" in html_body and "Relate" in html_body
    assert "Reference" not in html_body


def test_abstract_visible_no_details_and_scores(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com",
                              trend=TREND, user_name="yan-zys")
    conn.close()
    assert "yan-zys，你好" in html_body
    assert "今日为你筛选出 3 篇论文" in html_body
    # 全模板禁用 <details>，英文摘要直接可见
    assert "<details" not in html_body
    assert "We present" in html_body and "Here we show" in html_body and "Our data" in html_body
    assert "为理解复杂脑的独立演化提供细胞层面的证据。" in html_body
    # 卡片：等级配色 + Rank + 总分；Part 1 列表也含分数
    assert "card-must" in html_body and "card-important" in html_body and "card-relate" in html_body
    assert "badge-must" in html_body and "badge-important" in html_body and "badge-relate" in html_body
    assert "Rank 1" in html_body and "Rank 3" in html_body
    assert ">7.2<" in html_body and ">5.8<" in html_body and ">3.4<" in html_body
    # Part 2 卡片不再含"中文一句话"
    assert "中文一句话" not in html_body
    # DOI / PubMed 链接
    assert "https://doi.org/10.1" in html_body
    assert "PubMed 链接" in html_body
    # Part 3 分块渲染：聚焦方向（一是）/ 共同趋势 / 对研究者的价值
    assert "聚焦方向" in html_body and "一是" in html_body
    assert "共同趋势" in html_body and "对研究者的价值" in html_body


def test_render_trend_blocks_fallback_overview_only():
    """降级趋势（仅 overview）也能渲染，不出空块。"""
    html_out = ge.render_trend_blocks({"overview": "降级文案", "directions": [],
                                       "common_trend": "", "value": ""})
    assert "降级文案" in html_out
    assert "聚焦方向" not in html_out
    assert "共同趋势" not in html_out
