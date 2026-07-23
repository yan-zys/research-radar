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


def _seed_db(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", "Single-cell atlas of octopus brain", "We present ...",
          "Doe J, Smith A", "bioRxiv", "2026-07-22", "10.1", "https://www.biorxiv.org/content/10.1"),
         ("pubmed:10.2", "Brain evolution in annelids", "Here we show ...",
          "Lee B", "Nature", "2026-07-22", "10.2", "https://pubmed.ncbi.nlm.nih.gov/2/")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, one_line_summary_cn) "
        "VALUES (?, ?, ?, ?, ?)",
        [("biorxiv:10.1", 50, 1, "核心方向高度相关", "构建章鱼脑单细胞图谱，揭示细胞类型演化。"),
         ("pubmed:10.2", 40, 1, "神经系统演化直接相关", "比较环节动物脑演化，支持共同起源假说。")],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-23", "biorxiv:10.1", 9.2, "Must Read"),
         ("2026-07-23", "pubmed:10.2", 8.1, "Important")],
    )
    conn.commit()
    return conn


def test_render_contains_titles_and_mailto(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com", summary="测试趋势总述")
    conn.close()
    assert "Single-cell atlas of octopus brain" in html_body
    assert "Brain evolution in annelids" in html_body
    assert "测试趋势总述" in html_body
    assert "mailto:test@example.com" in html_body
    assert "rating%3Dgood" in html_body and "rating%3Dbad" in html_body  # mailto 主题已 URL 编码
    assert "paper_id%3Dbiorxiv%3A10.1" in html_body  # 主题中 paper_id 已 URL 编码
    assert "Must Read" in html_body and "Important" in html_body


def test_render_three_part_structure(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com",
                              summary="测试趋势总述", user_name="yan-zys")
    conn.close()
    assert "Part 1 · 今日论文新闻摘要" in html_body
    assert "Part 2 · 论文详情" in html_body
    assert "Part 3 · 今日推荐文献价值总结" in html_body
    assert "yan-zys，你好" in html_body
    assert "今日为你筛选出 2 篇论文" in html_body
    assert "构建章鱼脑单细胞图谱，揭示细胞类型演化。" in html_body  # Part 1 一句话
    assert "badge-must" in html_body and "badge-important" in html_body  # 分级徽标颜色
