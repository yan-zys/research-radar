"""expand_keywords 的最小可行测试（不调用 AI API）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from keyword_engine.expand_keywords import collect_input_keywords, normalize


def test_normalize():
    assert normalize("Hi-C Scaffolding") == normalize("hic scaffolding")
    assert normalize("Trio-Binning") == "triobinning"
    assert normalize("") == ""


def test_collect_input_keywords():
    profile = {"species": ["Octopus sinensis"], "tools": ["SAMap"], "exclude": None}
    kws = collect_input_keywords(profile)
    assert "octopussinensis" in kws
    assert "samap" in kws
