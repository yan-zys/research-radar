"""generate_digest 周报/月报测试（临时 DB，无网络/AI）。"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "generate_digest", ROOT / "email" / "generate_digest.py")
gd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gd)

TREND = {"overview": "本周重点集中于脑演化与单细胞图谱", "common_trend": "共同技术趋势：跨物种比较",
         "leads": ["线索一：跟踪 SAMap 应用", "线索二：跟踪神经肽注释"]}


def _seed_db(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", "Single-cell atlas of octopus brain", "We present ...",
          "Doe J", "bioRxiv", "2026-07-20", "10.1", "https://www.biorxiv.org/content/10.1"),
         ("pubmed:10.2", "Brain evolution in annelids", "Here we show ...",
          "Lee B", "Nature", "2026-07-21", "10.2", "https://pubmed.ncbi.nlm.nih.gov/2/"),
         ("pubmed:10.3", "Organ evolution in flatworms", "Our data ...",
          "Wang C", "eLife", "2026-07-22", "10.3", "https://pubmed.ncbi.nlm.nih.gov/3/"),
         ("pubmed:10.4", "Old paper outside window", "Ancient ...",
          "Zhao D", "bioRxiv", "2026-06-01", "10.4", "https://pubmed.ncbi.nlm.nih.gov/4/")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, reason, title_cn, "
        "one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("biorxiv:10.1", 50, 1, "r1", "章鱼脑图谱", "一句话一", "摘要一"),
         ("pubmed:10.2", 40, 1, "r2", "环节动物脑演化", "一句话二", "摘要二"),
         ("pubmed:10.3", 20, 1, "r3", "扁形动物器官演化", "一句话三", "摘要三"),
         ("pubmed:10.4", 10, 1, "r4", "旧论文", "一句话四", "摘要四")],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-20", "biorxiv:10.1", 6.0, "Important"),   # 同一 paper 两天推荐
         ("2026-07-23", "biorxiv:10.1", 7.2, "Must Read"),   # 去重后只留 7.2
         ("2026-07-23", "pubmed:10.2", 5.8, "Important"),
         ("2026-07-24", "pubmed:10.3", 3.4, "Relate"),
         ("2026-07-01", "pubmed:10.4", 9.9, "Must Read")],   # 7 天窗口外、30 天窗口内
    )
    conn.commit()
    return conn


def test_select_dedup_and_window(tmp_path):
    """同一 paper_id 只留最高分；7 天窗口不含 2026-07-01，30 天窗口含。"""
    conn = _seed_db(tmp_path)
    rows7 = gd.select_digest_papers(conn, 7, end_date="2026-07-24")
    ids7 = {r["paper_id"]: r["total_score"] for r in rows7}
    assert ids7 == {"biorxiv:10.1": 7.2, "pubmed:10.2": 5.8, "pubmed:10.3": 3.4}
    rows30 = gd.select_digest_papers(conn, 30, end_date="2026-07-24")
    ids30 = {r["paper_id"]: r["total_score"] for r in rows30}
    assert len(ids30) == 4 and ids30["pubmed:10.4"] == 9.9
    # 按总分降序
    assert [r["total_score"] for r in rows30] == sorted(
        (r["total_score"] for r in rows30), reverse=True)
    conn.close()


def test_compute_digest_stats(tmp_path):
    conn = _seed_db(tmp_path)
    rows = gd.select_digest_papers(conn, 7, end_date="2026-07-24")
    from processing.keyword_filter import load_config
    stats = gd.compute_digest_stats(rows, load_config())
    conn.close()
    assert stats["total"] == 3
    assert stats["grade_dist"] == {"Must Read": 1, "Important": 1, "Relate": 1}
    assert stats["tier_dist"] == {"顶刊": 1, "领域权威": 1, "其他": 1}  # Nature/eLife/bioRxiv
    assert dict(stats["top_journals"]) == {"Nature": 1, "bioRxiv": 1, "eLife": 1}
    assert isinstance(stats["top_keywords"], list)


def test_build_html_three_parts_no_details(tmp_path):
    conn = _seed_db(tmp_path)
    html_body, rows, date_range = gd.build_html(conn, 7, end_date="2026-07-24", trend=TREND)
    conn.close()
    assert date_range == "2026-07-18 ~ 2026-07-24"
    assert len(rows) == 3
    assert "Part 1 · 本周文献趋势总结" in html_body
    assert "Part 2 · 推荐分布统计" in html_body
    assert "Part 3 · 重点论文清单" in html_body
    assert "<details" not in html_body
    # 趋势分块：总览 / 共同技术趋势 / 下周跟踪线索（一是/二是）
    assert "本周重点集中于脑演化与单细胞图谱" in html_body
    assert "共同技术趋势" in html_body
    assert "下周跟踪线索" in html_body and "一是" in html_body and "二是" in html_body
    # 统计：收录 3 篇，定级分布含 Relate
    assert "共 <b>3</b> 篇" in html_body
    assert "Must Read <b>1</b>" in html_body and "Relate <b>1</b>" in html_body
    assert "主要来源期刊" in html_body and "高频关键词" in html_body
    # 清单：徽标 + 分数 + 标题 + 一句话 + 期刊·日期；去重后分数是 7.2
    assert ">7.2<" in html_body and ">6.0<" not in html_body
    assert "Single-cell atlas of octopus brain" in html_body
    assert "一句话一" in html_body
    assert "badge-must" in html_body and "badge-relate" in html_body


def test_monthly_subject_meta():
    assert gd.period_meta(7)["subject"] == "每周科研趋势"
    assert gd.period_meta(30)["subject"] == "每月科研趋势"
    assert gd.period_meta(30)["leads_title"] == "下月跟踪线索"
