"""scoring 的 grade 分档、期刊分档与加权计算测试（无网络/AI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ranking.scoring import compute_scores, grade_of, journal_score

WEIGHTS = {"research_relevance": 0.5, "ai_semantic_relevance": 0.2,
           "journal_influence": 0.15, "method_transfer_value": 0.1, "trend_value": 0.05}

KW = {"keywords": {
        "methods": {"weight": 8, "items": [{"keyword": "scRNA-seq", "weight": 1}]},
        "tools": {"weight": 9, "items": [{"keyword": "SAMap", "weight": 2}]},
        "concept": {"weight": 10, "items": [{"keyword": "evolution", "weight": 1}]}},
      "negative": []}


def test_grade_boundaries():
    assert grade_of(9.0) == "Must Read"
    assert grade_of(8.5) == "Important"
    assert grade_of(5.0) == "Reference"
    assert grade_of(4.99) is None


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
    # a：rule/method/trend 均为池内最大 → 归一化 10；ai=8；期刊 10
    assert a["total_score"] == round(10 * 0.5 + 8 * 0.2 + 10 * 0.15 + 10 * 0.1 + 10 * 0.05, 2)
    assert a["grade"] == "Must Read"
    # b：rule 归一化 50/100*10=5；ai 缺失按 5；bioRxiv=4；无 method 命中；trend 归一化 10
    assert b["total_score"] == round(5 * 0.5 + 5 * 0.2 + 4 * 0.15 + 0 * 0.1 + 10 * 0.05, 2)
    assert b["grade"] is None  # <5 不入库
