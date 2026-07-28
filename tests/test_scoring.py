"""scoring 的 grade 分档、期刊分档与加权计算测试（无网络/AI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db  # noqa: E402
from ranking.scoring import compute_scores, grade_of, journal_score, run  # noqa: E402

WEIGHTS = {"research_relevance": 0.5, "ai_semantic_relevance": 0.2,
           "journal_influence": 0.2, "trend_value": 0.1}

KW = {"keywords": {
        "methods": {"weight": 8, "items": [{"keyword": "scRNA-seq", "weight": 1}]},
        "tools": {"weight": 9, "items": [{"keyword": "SAMap", "weight": 2}]},
        "concept": {"weight": 10, "items": [{"keyword": "evolution", "weight": 1}]}},
      "negative": []}


def test_grade_boundaries():
    assert grade_of(7.0) == "Must Read"
    assert grade_of(6.99) == "Important"
    assert grade_of(5.0) == "Important"
    assert grade_of(4.99) == "Relate"
    assert grade_of(0.0) == "Relate"  # 低分也入选，等级为 Relate


def test_journal_tiers():
    assert journal_score("Nature Methods") == 10.0
    assert journal_score("Science Advances") == 10.0
    assert journal_score("Cell") == 10.0
    assert journal_score("eLife") == 7.0
    assert journal_score("PLOS Biology") == 7.0
    assert journal_score("bioRxiv") == 4.0
    assert journal_score("Some Obscure Journal") == 5.0


def test_weighted_total_and_normalization():
    rows = [
        {"paper_id": "x:1", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 8},
        {"paper_id": "x:2", "title": "evolution", "abstract": "",
         "journal": "bioRxiv", "rule_score": 50, "ai_score": None},
    ]
    scored = {e["paper_id"]: e for e in compute_scores(rows, WEIGHTS, KW)}
    a, b = scored["x:1"], scored["x:2"]
    # a：rule/trend 均为池内最大 → 归一化 10；ai=8；期刊 10
    assert a["total_score"] == round(10 * 0.5 + 8 * 0.2 + 10 * 0.2 + 10 * 0.1, 2)
    assert a["grade"] == "Must Read"
    # b：rule 归一化 50/100*10=5；ai 缺失按 5；bioRxiv=4；trend 归一化 10 → 总分 5.3
    assert b["total_score"] == round(5 * 0.5 + 5 * 0.2 + 4 * 0.2 + 10 * 0.1, 2)
    assert b["grade"] == "Important"  # ≥5 且 <7


def test_top15_all_selected_no_score_cutoff(tmp_path):
    """低分论文也进入 Top 推荐（不再因 <5 剔除），grade 为 Relate。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("x:high", "SAMap evolution scRNA-seq", "", "Nature"),
         ("x:low", "evolution", "", "bioRxiv")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:high", 100, 1, 9), ("x:low", 1, 1, 3)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24")
    ids = {e["paper_id"]: e["grade"] for e in top}
    assert ids["x:high"] == "Must Read"
    assert ids["x:low"] == "Relate"  # 低分不剔除
    grades = {r[0] for r in conn.execute(
        "SELECT grade FROM recommendations WHERE date = '2026-07-24'")}
    assert grades == {"Must Read", "Relate"}
    conn.close()


def _seed_run_db(tmp_path):
    """三篇通过过滤的论文；x:recent 近 30 天内已推荐过，x:old 是 40 天前推荐的。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("x:new", "SAMap evolution scRNA-seq", "", "Nature"),
         ("x:recent", "SAMap evolution scRNA-seq recent", "", "Nature"),
         ("x:old", "SAMap evolution scRNA-seq old", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:new", 100, 1, 9), ("x:recent", 100, 1, 9), ("x:old", 100, 1, 9)],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-10", "x:recent", 9.5, "Must Read"),   # run_date 前 13 天，应被去重
         ("2026-06-13", "x:old", 9.5, "Must Read")],      # run_date 前 40 天，超出窗口
    )
    conn.commit()
    return conn


def test_ai_veto_excludes_low_relevance():
    """ai_score 已评且 <3 的论文被 AI 否决，不进入推荐；未评分（None）不受影响。"""
    rows = [
        {"paper_id": "x:ok", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 8},
        {"paper_id": "x:veto", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 0},
    ]
    scored = {e["paper_id"]: e for e in compute_scores(rows, WEIGHTS, KW)}
    assert "x:ok" in scored
    assert "x:veto" not in scored


def test_run_pub_date_filters_candidates(tmp_path):
    """pub_date 限定候选论文的发表日期（papers.date）：补算某日发表文献的
    历史日报时，只从该日发表的论文中选取；默认 None 不过滤。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal, date) "
        "VALUES (?, ?, ?, ?, ?)",
        [("x:0722", "SAMap evolution scRNA-seq a", "", "Nature", "2026-07-22"),
         ("x:0723", "SAMap evolution scRNA-seq b", "", "Nature", "2026-07-23")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:0722", 100, 1, 9), ("x:0723", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-22", pub_date="2026-07-22")
    assert [e["paper_id"] for e in top] == ["x:0722"]
    # 默认不带 pub_date：两天发表的论文都是候选（x:0722 已被推荐过，落 D 层）
    top_all = run(conn, WEIGHTS, KW, run_date="2026-07-24")
    assert {e["paper_id"] for e in top_all} == {"x:0722", "x:0723"}
    conn.close()


def test_run_dedupes_last_30_days(tmp_path):
    """A 层仍做 30 天去重；去重被排除的论文只在 D 兜底层按序回补（排在 A 层之后）。"""
    conn = _seed_run_db(tmp_path)
    top = run(conn, WEIGHTS, KW, run_date="2026-07-23")
    by_id = {e["paper_id"]: e for e in top}
    assert by_id["x:new"]["tier"] == "A"
    assert by_id["x:old"]["tier"] == "A"          # 超过 30 天窗口，回 A 层
    assert by_id["x:recent"]["tier"] == "D"       # 近 30 天已推荐，仅 D 层兜底
    assert [e["paper_id"] for e in top][:2] == ["x:new", "x:old"]  # A 层排前
    assert top[-1]["paper_id"] == "x:recent"
    conn.close()


def test_tiered_fallback_fills_top15(tmp_path):
    """A 层只有 2 篇时，B/C 层补足 15 篇：tier 标记正确且 A 层排在最前。
    B 层只收顶刊清单内且已评 AI 的论文；C 层收被否决的关键词/顶刊论文。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    papers = [("a:1", "SAMap evolution scRNA-seq one", "", "Nature"),
              ("a:2", "SAMap evolution scRNA-seq two", "", "eLife")]
    papers += [(f"b:{i}", f"unrelated topic {i}", "", "Nature communications")
               for i in range(8)]
    papers += [(f"c:{i}", f"vetoed topic {i}", "", "Nature") for i in range(5)]
    papers += [("n:1", "noise not top journal", "", "Some Obscure Journal")]
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        papers,
    )
    scores = [("a:1", 100, 1, 9), ("a:2", 80, 1, 8)]
    scores += [(f"b:{i}", 0, 0, 5) for i in range(8)]   # 顶刊、已评 AI、未否决
    scores += [(f"c:{i}", 50, 1, 1) for i in range(5)]  # 被 AI 否决
    scores += [("n:1", 99, 0, None)]                    # 非顶刊+规则未命中 → 不推荐
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        scores,
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24")
    conn.close()
    assert len(top) == 15
    assert "n:1" not in {e["paper_id"] for e in top}
    tiers = [e["tier"] for e in top]
    assert tiers == ["A", "A"] + ["B"] * 8 + ["C"] * 5
    assert {e["paper_id"] for e in top[:2]} == {"a:1", "a:2"}
    # 层内按 total_score 降序
    for t in ("A", "B", "C"):
        layer = [e["total_score"] for e in top if e["tier"] == t]
        assert layer == sorted(layer, reverse=True)


def test_tiered_fallback_excludes_negative(tmp_path):
    """梯队兜底（B/C 层）也要剔除命中 negative 排除词的论文
    （医学/临床噪声，含此类词汇的邮件易触发 SMTP 550 内容过滤）。"""
    kw = {"keywords": KW["keywords"], "negative": ["cancer"]}
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    papers = [("a:1", "SAMap evolution scRNA-seq one", "", "Nature"),
              ("b:ok", "unrelated topic", "", "Nature communications"),
              ("b:neg", "unrelated cancer topic", "", "Nature communications"),
              ("c:neg", "vetoed cancer topic", "", "Nature")]
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        papers,
    )
    scores = [("a:1", 100, 1, 9), ("b:ok", 0, 0, 6),
              ("b:neg", 99, 0, 6), ("c:neg", 99, 1, 1)]
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        scores,
    )
    conn.commit()
    top = run(conn, WEIGHTS, kw, run_date="2026-07-24")
    conn.close()
    ids = [e["paper_id"] for e in top]
    assert ids == ["a:1", "b:ok"]  # negative 命中的 b:neg / c:neg 均被剔除，不兜底
