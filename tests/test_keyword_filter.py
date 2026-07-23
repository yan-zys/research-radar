"""keyword_filter 匹配与计分逻辑测试（无网络/AI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db
from processing.keyword_filter import group_scores, match_keywords, rule_score, run

CONFIG = {
    "keywords": {
        "species": {"weight": 5, "items": [{"keyword": "octopus", "weight": 2}]},
        "methods": {"weight": 8, "items": [{"keyword": "single-cell RNA sequencing", "weight": 2},
                                           {"keyword": "deep learning"}]},  # 词条 weight 缺省按 1
        "concept": {"weight": 10, "items": [{"keyword": "brain evolution", "weight": 2}]},
    },
    "negative": ["cancer"],
}


def test_match_case_insensitive_and_weights():
    m = match_keywords("Octopus Brain Evolution",
                       "We used Single-Cell RNA Sequencing and deep learning.", CONFIG)
    scores = {h["keyword"]: h["score"] for h in m["hits"]}
    assert scores["octopus"] == 5 * 2
    assert scores["single-cell RNA sequencing"] == 8 * 2
    assert scores["deep learning"] == 8 * 1  # 缺省 weight 按 1
    assert scores["brain evolution"] == 10 * 2
    assert rule_score(m) == 10 + 16 + 8 + 20


def test_negative_rejects():
    m = match_keywords("octopus cancer study", "", CONFIG)
    assert m["negative"] == ["cancer"]
    assert rule_score(m) == 0.0


def test_group_scores():
    m = match_keywords("octopus single-cell RNA sequencing", "", CONFIG)
    assert group_scores(m) == {"species": 10, "methods": 16}


def test_run_writes_paper_scores(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract) VALUES (?, ?, ?)",
        [("a:1", "octopus brain", "single-cell RNA sequencing study"),
         ("a:2", "unrelated topic", "nothing here"),
         ("a:3", "octopus cancer", "")],
    )
    stats = run(conn, CONFIG)
    assert stats == {"total": 3, "passed": 1, "rejected": 2}
    rows = {r["paper_id"]: r for r in conn.execute("SELECT * FROM paper_scores")}
    assert rows["a:1"]["passed_filter"] == 1 and rows["a:1"]["rule_score"] == 26
    assert rows["a:2"]["passed_filter"] == 0 and rows["a:2"]["rule_score"] == 0
    assert rows["a:3"]["passed_filter"] == 0
    conn.close()
