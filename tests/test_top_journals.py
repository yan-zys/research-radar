"""顶刊通道（crawler/top_journals）与 analyzer 顶刊候选选取的测试（无网络/AI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.top_journals import (build_journal_query, is_research_article,  # noqa: E402
                                  journal_names, load_journals)
from database.db import get_conn, init_db  # noqa: E402
from processing.paper_analyzer import select_top_journal_candidates  # noqa: E402

JOURNALS = [
    {"name": "Nature", "issn": "0028-0836"},
    {"name": "Science (New York, N.Y.)", "issn": "0036-8075"},
]


def test_load_journals_real_config():
    journals = load_journals()
    assert len(journals) >= 10
    assert all(j["name"] and j["issn"] for j in journals)


def test_build_journal_query():
    q = build_journal_query(JOURNALS)
    assert q == '("0028-0836"[issn] OR "0036-8075"[issn])'


def test_journal_names_lowercase(tmp_path):
    p = tmp_path / "tj.yaml"
    p.write_text("journals:\n"
                 "  - name: Nature\n    issn: '0028-0836'\n"
                 "  - name: Science (New York, N.Y.)\n    issn: '0036-8075'\n",
                 encoding="utf-8")
    assert journal_names(p) == ["nature", "science (new york, n.y.)"]


def _seed(conn):
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal, date) VALUES (?, ?, ?, ?, ?)",
        [("tj:1", "new nature paper", "abs", "Nature", "2026-07-23"),
         ("tj:2", "old nature paper", "abs", "nature", "2026-07-20"),
         ("tj:3", "scored nature paper", "abs", "Nature", "2026-07-24"),
         ("ot:1", "obscure journal paper", "abs", "Some Obscure Journal", "2026-07-24")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) VALUES (?, ?, ?, ?)",
        [("tj:1", 0, 0, None),
         ("tj:2", 0, 0, None),
         ("tj:3", 0, 0, 8.0),
         ("ot:1", 0, 0, None)],
    )
    conn.commit()


def test_select_top_journal_candidates(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed(conn)
    rows = select_top_journal_candidates(conn, 10)
    ids = [r["paper_id"] for r in rows]
    # 期刊名大小写不敏感；已评 AI 的排除；非顶刊排除；日期降序
    assert ids == ["tj:1", "tj:2"]
    conn.close()


def test_select_top_journal_candidates_limit_and_empty(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed(conn)
    assert [r["paper_id"] for r in select_top_journal_candidates(conn, 1)] == ["tj:1"]
    assert select_top_journal_candidates(conn, 0) == []
    conn.close()


def test_select_top_journal_candidates_prestige_first(tmp_path):
    """低分档顶刊的新论文排在高分档顶刊的旧论文之后（eLife=7 < Nature=10）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal, date) VALUES (?, ?, ?, ?, ?)",
        [("elife:new", "new elife", "abs", "eLife", "2026-07-25"),
         ("nat:old", "old nature", "abs", "Nature", "2026-07-18")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) VALUES (?, 0, 0, NULL)",
        [("elife:new",), ("nat:old",)],
    )
    conn.commit()
    rows = select_top_journal_candidates(conn, 10)
    assert [r["paper_id"] for r in rows] == ["nat:old", "elife:new"]
    conn.close()


def test_is_research_article():
    research = {"title": "Novel cortex-wide signaling", "doi": "10.1038/s41586-026-1",
                "pub_types": ["Journal Article"]}
    assert is_research_article(research)
    assert is_research_article({"title": "A review", "doi": "", "pub_types": ["Review"]})
    # PublicationType 拦截
    assert not is_research_article({"title": "X", "doi": "10.1038/s41586-026-2",
                                    "pub_types": ["News"]})
    assert not is_research_article({"title": "X", "doi": "", "pub_types": ["Editorial", "Comment"]})
    # Nature 新闻版块 DOI
    assert not is_research_article({"title": "White House rolls out AI funding",
                                    "doi": "10.1038/d41586-026-02280-3",
                                    "pub_types": ["Journal Article"]})
    # 标题兜底（pub_types 缺失）
    assert not is_research_article({"title": "Author Correction: Feature-specific threat",
                                    "doi": "10.1038/s41586-026-10911-y", "pub_types": []})
    assert not is_research_article({"title": "Daily briefing: Orcas smash sunfish",
                                    "doi": "", "pub_types": []})
