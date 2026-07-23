"""review_candidates / merge_to_config 的最小可行测试（不调用 AI API）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keyword_engine.merge_to_config import classify, seed_items
from keyword_engine.review_candidates import apply_decision

PROFILE = {
    "species": ["octopus"],
    "methods": ["single-cell RNA sequencing"],
    "tools": ["SAMap"],
    "research_interest": ["brain evolution"],
    "keywords": [],
    "exclude": None,
}


def test_apply_decision():
    c = {"keyword": "x", "status": "pending"}
    assert apply_decision(c, "y")["status"] == "approved"
    assert apply_decision(c, "n")["status"] == "rejected"
    assert apply_decision(c, "s")["status"] == "pending"


def test_classify():
    assert classify("octopus vulgaris", PROFILE) == "species"
    assert classify("SAMap v2", PROFILE) == "tools"
    assert classify("single-cell RNA sequencing analysis", PROFILE) == "methods"
    assert classify("pangenome", PROFILE) == "concept"


def test_seed_items():
    items = dict((kw, g) for kw, g in seed_items(PROFILE))
    assert items["octopus"] == "species"
    assert items["SAMap"] == "tools"
    assert items["brain evolution"] == "concept"
