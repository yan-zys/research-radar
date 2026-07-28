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
    """同一 paper_id 只留最高分；窗口内不足 15 时先补窗口外更早的 recommendations。"""
    conn = _seed_db(tmp_path)
    rows7 = gd.select_digest_papers(conn, 7, end_date="2026-07-24", offline=True)
    ids7 = {r["paper_id"]: r["total_score"] for r in rows7}
    # 7 天窗口内 3 篇在前，窗口外的 pubmed:10.4（2026-07-01）兜底补在后
    assert ids7 == {"biorxiv:10.1": 7.2, "pubmed:10.2": 5.8,
                    "pubmed:10.3": 3.4, "pubmed:10.4": 9.9}
    assert [r["paper_id"] for r in rows7][:3] == ["biorxiv:10.1", "pubmed:10.2",
                                                  "pubmed:10.3"]
    assert rows7[-1]["paper_id"] == "pubmed:10.4"
    rows30 = gd.select_digest_papers(conn, 30, end_date="2026-07-24", offline=True)
    ids30 = {r["paper_id"]: r["total_score"] for r in rows30}
    assert len(ids30) == 4 and ids30["pubmed:10.4"] == 9.9
    # 30 天窗口无需兜底，整体按总分降序
    assert [r["total_score"] for r in rows30] == sorted(
        (r["total_score"] for r in rows30), reverse=True)
    conn.close()


def test_pool_fallback_fills_15(tmp_path):
    """窗口内外 recommendations 合计不足 15 时，从 scoring 池按总分补足 15 篇。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    papers = [("rec:1", "octopus brain atlas", "abs", "Nature"),
              ("rec:2", "annelid brain evolution", "abs", "eLife")]
    papers += [(f"pool:{i}", f"paper {i}", f"abstract {i}", "bioRxiv")
               for i in range(20)]
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        papers,
    )
    scores = [("rec:1", 50, 1), ("rec:2", 40, 1)]
    scores += [(f"pool:{i}", i, 1) for i in range(20)]
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter) VALUES (?, ?, ?)",
        scores,
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-23", "rec:1", 6.0, "Important"),
         ("2026-07-24", "rec:2", 5.0, "Important")],
    )
    conn.commit()
    rows = gd.select_digest_papers(conn, 7, end_date="2026-07-24", offline=True)
    conn.close()
    assert len(rows) == 15
    assert [r["paper_id"] for r in rows[:2]] == ["rec:1", "rec:2"]  # 窗口内推荐在前
    pool_rows = rows[2:]
    assert all(r["paper_id"].startswith("pool:") for r in pool_rows)
    assert len({r["paper_id"] for r in rows}) == 15  # 无重复
    for r in pool_rows:  # 池选论文有算分得到的 grade/total_score，字段结构一致
        assert r["total_score"] is not None
        assert r["grade"] in ("Must Read", "Important", "Relate")
        assert r["rec_date"] is None
    pool_scores = [r["total_score"] for r in pool_rows]
    assert pool_scores == sorted(pool_scores, reverse=True)


def test_compute_digest_stats(tmp_path):
    conn = _seed_db(tmp_path)
    rows = gd.select_digest_papers(conn, 7, end_date="2026-07-24", offline=True)
    from processing.keyword_filter import load_config
    stats = gd.compute_digest_stats(rows, load_config())
    conn.close()
    assert stats["total"] == 4  # 窗口内 3 篇 + 窗口外兜底 1 篇
    assert stats["grade_dist"] == {"Must Read": 2, "Important": 1, "Relate": 1}
    assert stats["tier_dist"] == {"顶刊": 1, "领域权威": 1, "其他": 2}  # Nature/eLife/bioRxiv×2
    assert dict(stats["top_journals"]) == {"bioRxiv": 2, "Nature": 1, "eLife": 1}
    assert isinstance(stats["top_keywords"], list)


def test_build_html_three_parts_no_details(tmp_path):
    conn = _seed_db(tmp_path)
    html_body, rows, date_range = gd.build_html(conn, 7, end_date="2026-07-24",
                                                trend=TREND, offline=True)
    conn.close()
    assert date_range == "2026-07-18 ~ 2026-07-24"
    assert len(rows) == 4  # 窗口内 3 篇 + 窗口外更早 recommendations 兜底 1 篇
    assert "Part 1 · 本周文献趋势总结" in html_body
    assert "Part 2 · 推荐分布统计" in html_body
    assert "Part 3 · 重点论文清单" in html_body
    assert "<details" not in html_body
    # 趋势分块：总览 / 共同技术趋势 / 下周跟踪线索（一是/二是）
    assert "本周重点集中于脑演化与单细胞图谱" in html_body
    assert "共同技术趋势" in html_body
    assert "下周跟踪线索" in html_body and "一是" in html_body and "二是" in html_body
    # 统计：收录 4 篇，定级分布含 Relate
    assert "共 <b>4</b> 篇" in html_body
    assert "Must Read <b>2</b>" in html_body and "Relate <b>1</b>" in html_body
    assert "主要来源期刊" in html_body and "高频关键词" in html_body
    # 清单：徽标 + 分数 + 标题 + 一句话 + 期刊·日期；去重后分数是 7.2
    assert ">7.2<" in html_body and ">6.0<" not in html_body
    assert "Single-cell atlas of octopus brain" in html_body
    assert "一句话一" in html_body
    assert "badge-must" in html_body and "badge-relate" in html_body
    # Part 3 增强：推荐理由 + 中文摘要 + 链接 + 一键反馈
    assert "推荐理由：" in html_body and "r1" in html_body
    assert "中文摘要：" in html_body and "摘要一" in html_body
    assert "PubMed 链接" in html_body and "https://doi.org/10.1" in html_body
    assert "feedback?paper_id=" in html_body and "rating=" in html_body


def test_negative_papers_excluded(tmp_path):
    """命中 negative 排除词的论文（真实配置含 cancer/clinical 等医学词）在
    窗口内、窗口外 recommendations 和 scoring 池兜底三处来源中均被剔除。"""
    conn = _seed_db(tmp_path)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("pubmed:99.1", "Cancer immunotherapy in patients", "tumor ...",
          "A B", "Nature", "2026-07-23", "99.1", "https://pubmed.ncbi.nlm.nih.gov/99/"),
         ("pubmed:99.2", "Old cancer trial", "clinical ...",
          "C D", "Nature", "2026-06-02", "99.2", "https://pubmed.ncbi.nlm.nih.gov/98/")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter) VALUES (?, ?, ?)",
        [("pubmed:99.1", 100, 1), ("pubmed:99.2", 100, 1)],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-23", "pubmed:99.1", 10.0, "Must Read"),   # 窗口内最高分，仍应剔除
         ("2026-06-02", "pubmed:99.2", 10.0, "Must Read")],   # 窗口外兜底，仍应剔除
    )
    conn.commit()
    rows = gd.select_digest_papers(conn, 30, end_date="2026-07-24", offline=True)
    ids = [r["paper_id"] for r in rows]
    conn.close()
    assert "pubmed:99.1" not in ids and "pubmed:99.2" not in ids
    assert "pubmed:10.4" in ids  # 非医学论文正常入选


def test_monthly_subject_meta():
    assert gd.period_meta(7)["subject"] == "每周科研趋势"
    assert gd.period_meta(30)["subject"] == "每月科研趋势"
    assert gd.period_meta(30)["leads_title"] == "下月跟踪线索"
