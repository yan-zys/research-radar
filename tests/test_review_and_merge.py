"""review_candidates / merge_to_config 的最小可行测试（不调用 AI API）。"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keyword_engine import merge_to_config  # noqa: E402
from keyword_engine.merge_to_config import classify, seed_items  # noqa: E402
from keyword_engine.review_candidates import apply_decision  # noqa: E402

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


def test_merge_approved_exclude_goes_to_negative(tmp_path, monkeypatch):
    """level: exclude 的 approved 候选进 config 的 negative（去重），不进正向关键词组。"""
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.dump(PROFILE, allow_unicode=True), encoding="utf-8")
    config_path = tmp_path / "keyword_config.yaml"
    config_path.write_text(yaml.dump({
        "keywords": {g: {"weight": w, "items": []} for g, w in
                     {"species": 5, "methods": 8, "tools": 9, "concept": 10}.items()},
        "negative": ["cancer"],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    cands_path = tmp_path / "candidates.yaml"
    cands_path.write_text(yaml.dump([
        {"keyword": "clinical trial", "level": "exclude", "status": "approved",
         "source": "feedback_reason"},
        {"keyword": "cancer", "level": "exclude", "status": "approved"},  # 已在 negative，应去重
        {"keyword": "octopus brain", "level": "A", "status": "approved"},  # 正常关键词
        {"keyword": "pending word", "status": "pending"},                  # 不处理
    ], allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["merge_to_config.py",
                                      "--candidates", str(cands_path),
                                      "--profile", str(profile_path),
                                      "--config", str(config_path)])
    merge_to_config.main()
    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["negative"] == ["cancer", "clinical trial"]
    kws = [i["keyword"] for g in merged["keywords"].values() for i in g["items"]]
    assert kws == ["octopus brain"]
