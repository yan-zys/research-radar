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
    # abstract_cn 预先填好，避免测试触发真实 AI 调用
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, "
        "one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", 50, 1, "核心方向高度相关", "构建章鱼脑单细胞图谱，揭示细胞类型演化。",
          "章鱼脑神经元类型缺乏系统图谱。作者用单细胞转录组测序构建全脑图谱，"
          "鉴定出数十种细胞类型，发现其与脊椎动物脑细胞存在深度同源，"
          "为理解复杂脑的独立演化提供细胞层面的证据。"),
         ("pubmed:10.2", 40, 1, "神经系统演化直接相关", "比较环节动物脑演化，支持共同起源假说。",
          "环节动物与脊椎动物脑的同源性长期存疑。作者比较多个环节动物物种的脑转录组，"
          "发现保守的神经发育调控模块，支持两侧对称动物脑的共同起源假说。")],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-23", "biorxiv:10.1", 9.2, "Must Read"),
         ("2026-07-23", "pubmed:10.2", 8.1, "Important")],
    )
    conn.commit()
    return conn


def test_render_card_structure_and_feedback_links(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com", summary="测试趋势总述")
    conn.close()
    assert "Single-cell atlas of octopus brain" in html_body
    assert "Brain evolution in annelids" in html_body
    assert "测试趋势总述" in html_body
    # 一键反馈链接指向本机小服务，不再是 mailto
    assert "mailto:" not in html_body
    assert "http://127.0.0.1:8710/feedback?paper_id=biorxiv%3A10.1&rating=good" in html_body
    assert "rating=ok" in html_body and "rating=bad" in html_body
    assert "感兴趣" in html_body and "一般" in html_body and "不相关" in html_body
    assert "Must Read" in html_body and "Important" in html_body


def test_render_cn_only_and_details(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com",
                              summary="测试趋势总述", user_name="yan-zys")
    conn.close()
    assert "yan-zys，你好" in html_body
    assert "今日为你筛选出 2 篇论文" in html_body
    # 卡片式：details 下拉 + 等级配色
    assert "<details><summary>详细解读</summary>" in html_body
    assert "card-must" in html_body and "card-important" in html_body
    assert "badge-must" in html_body and "badge-important" in html_body
    # 中文内容齐全
    assert "构建章鱼脑单细胞图谱，揭示细胞类型演化。" in html_body
    assert "为理解复杂脑的独立演化提供细胞层面的证据。" in html_body
    # 全卡片不出现英文摘要
    assert "We present" not in html_body
    assert "Here we show" not in html_body
    # 顶部统计条：昨日（2026-07-22）入库 2 篇，通过过滤 2 篇
    assert "stats-bar" in html_body
    assert "入库 <b>2</b> 篇，通过过滤 <b>2</b> 篇" in html_body
    assert "物种" in html_body and "方法" in html_body and "工具" in html_body and "概念" in html_body
