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
from processing.keyword_filter import load_config  # noqa: E402

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
    # abstract_cn/title_cn 预先填好，避免测试触发真实 AI 调用
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, title_cn, "
        "one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", 50, 1, "核心方向高度相关", "章鱼脑单细胞图谱",
          "构建章鱼脑单细胞图谱，揭示细胞类型演化。",
          "章鱼脑神经元类型缺乏系统图谱。作者用单细胞转录组测序构建全脑图谱，"
          "鉴定出数十种细胞类型，发现其与脊椎动物脑细胞存在深度同源，"
          "为理解复杂脑的独立演化提供细胞层面的证据。"),
         ("pubmed:10.2", 40, 1, "神经系统演化直接相关", "环节动物脑演化",
          "比较环节动物脑演化，支持共同起源假说。",
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


def test_four_part_structure_and_feedback_links(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com", summary="测试趋势总述")
    conn.close()
    # 四段式结构齐全
    assert "Part 1 · 今日文献趋势总结" in html_body
    assert "Part 2 · 今日推荐分布统计" in html_body
    assert "Part 3 · 今日重点论文清单" in html_body
    assert "Part 4 · 论文详细信息卡片" in html_body
    assert "测试趋势总述" in html_body
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
    assert "Must Read" in html_body and "Important" in html_body


def test_bilingual_abstract_and_stats(tmp_path):
    conn = _seed_db(tmp_path)
    html_body = ge.build_html("2026-07-23", conn, "test@example.com",
                              summary="测试趋势总述", user_name="yan-zys")
    conn.close()
    assert "yan-zys，你好" in html_body
    assert "今日为你筛选出 2 篇论文" in html_body
    # 双语摘要：中文默认显示，英文原文收进 <details> 折叠
    assert "<details><summary>英文摘要（原文）</summary>" in html_body
    assert "We present" in html_body and "Here we show" in html_body
    assert "为理解复杂脑的独立演化提供细胞层面的证据。" in html_body
    # 卡片：等级配色 + Rank + 总分
    assert "card-must" in html_body and "card-important" in html_body
    assert "badge-must" in html_body and "badge-important" in html_body
    assert "Rank 1" in html_body and "Rank 2" in html_body
    # Part 2 统计：定级分布 / 期刊分层 / 来源期刊 / 高频关键词 / 昨日一行
    assert "Must Read <b>1</b>" in html_body
    assert "Important <b>1</b>" in html_body
    assert "Reference <b>0</b>" in html_body
    assert "顶刊 <b>1</b>" in html_body      # Nature 算顶刊
    assert "其他 <b>1</b>" in html_body      # bioRxiv 算其他
    assert "主要来源期刊" in html_body
    assert "高频关键词" in html_body
    assert "入库 <b>2</b> 篇，通过过滤 <b>2</b> 篇" in html_body


def test_compute_daily_stats_pure_aggregation(tmp_path):
    conn = _seed_db(tmp_path)
    rows = ge.fetch_rows("2026-07-23", conn)
    stats = ge.compute_daily_stats("2026-07-23", conn, rows, load_config())
    conn.close()
    assert stats["grade_dist"] == {"Must Read": 1, "Important": 1, "Reference": 0}
    assert stats["tier_dist"] == {"顶刊": 1, "领域权威": 0, "其他": 1}
    assert dict(stats["top_journals"]) == {"Nature": 1, "bioRxiv": 1}
    assert stats["yesterday"] == {"date": "2026-07-22", "total": 2, "passed": 2}
    assert isinstance(stats["top_keywords"], list)


def test_quick_list_fallback_note_when_no_key_papers(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.execute(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date) "
        "VALUES ('x:1', 'Some paper', 'abs', 'A', 'eLife', '2026-07-22')")
    conn.execute(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, title_cn, "
        "one_line_summary_cn, abstract_cn) VALUES ('x:1', 10, 1, 'r', '某论文', '一句话', '摘要')")
    conn.execute(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) "
        "VALUES ('2026-07-23', 'x:1', 6.0, 'Reference')")
    conn.commit()
    html_body = ge.build_html("2026-07-23", conn, "test@example.com", summary="s")
    conn.close()
    assert "今日无 Must Read/Important，以下为评分最高 5 篇" in html_body
