"""keyword_engine/apply_page_requests.py 的入库逻辑测试（网络与 AI 全部 mock）。"""
import json
import sys
import types
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from keyword_engine import apply_page_requests as apr  # noqa: E402

CONFIG = {
    "keywords": {
        "core": {"weight": 15, "items": []},
        "concept": {"weight": 10, "items": [
            {"keyword": "brain evolution", "source": "seed",
             "added_date": "2026-07-01", "weight": 2, "locked": True},
        ]},
        "methods": {"weight": 8, "items": []},
        "species": {"weight": 5, "items": []},
    },
    "negative": [],
}
PROFILE = {"species": ["octopus"], "methods": ["single-cell RNA sequencing"],
           "tools": [], "research_interest": [], "keywords": []}


def _setup(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump(CONFIG, allow_unicode=True), encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.dump(PROFILE, allow_unicode=True), encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    return types.SimpleNamespace(
        inbox=str(inbox), config=str(config), profile=str(profile),
        archive=str(tmp_path / "archive.yaml"),
        processed_log=str(tmp_path / "processed.log"), max_new=10), config, inbox


def _items(config_path, group):
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return data["keywords"][group]["items"]


def test_keywords_request_seeded_with_top_weight(tmp_path):
    """手写关键词：拆分、去重、归类，以 weight=2 locked 入库；已存在词跳过。"""
    args, config, inbox = _setup(tmp_path)
    (inbox / "2026-08-21.jsonl").write_text(
        json.dumps({"type": "keywords",
                    "text": "single-cell RNA sequencing, brain evolution，sea cucumber"},
                ensure_ascii=False) + "\n", encoding="utf-8")
    assert apr.run(args) == 0
    methods = _items(config, "methods")
    concept = _items(config, "concept")
    assert [i["keyword"] for i in methods] == ["single-cell RNA sequencing"]
    assert methods[0]["weight"] == 2 and methods[0]["locked"] is True
    assert methods[0]["source"] == "user_page"
    # brain evolution 已在库，跳过；sea cucumber 归不进画像组 → concept
    assert [i["keyword"] for i in concept] == ["brain evolution", "sea cucumber"]
    # 处理后文件被记录，重跑不再处理
    assert "2026-08-21.jsonl" in Path(args.processed_log).read_text(encoding="utf-8")
    assert apr.run(args) == 0
    assert [i["keyword"] for i in _items(config, "methods")] == ["single-cell RNA sequencing"]


def test_papers_request_mined_keywords_low_weight(tmp_path, monkeypatch):
    """文献请求：DOI/PMID 解析 + 一次 AI 提炼，以 weight=1 非 locked 入库。"""
    args, config, inbox = _setup(tmp_path)
    (inbox / "2026-08-21.jsonl").write_text(
        json.dumps({"type": "papers", "ids": ["40123456", "10.1038/abc"]},
                ensure_ascii=False) + "\n", encoding="utf-8")
    calls = {"resolve": [], "ai": 0}

    def fake_resolve(pid):
        calls["resolve"].append(pid)
        return {"title": f"Paper {pid}", "abstract": "octopus brain single-cell atlas"}

    def fake_mine(papers, existing, max_new):
        calls["ai"] += 1
        assert len(papers) == 2
        return ["cell type evolution", "brain evolution", "neuropeptide signaling"]

    monkeypatch.setattr(apr, "resolve_paper", fake_resolve)
    monkeypatch.setattr(apr, "mine_keywords_from_papers", fake_mine)
    assert apr.run(args) == 0
    assert calls["resolve"] == ["40123456", "10.1038/abc"]
    assert calls["ai"] == 1  # 两篇汇总只调一次 AI
    concept = _items(config, "concept")
    kws = [i["keyword"] for i in concept]
    assert "cell type evolution" in kws and "neuropeptide signaling" in kws
    assert kws.count("brain evolution") == 1  # 库中已有，提炼结果去重不重复入库
    added = next(i for i in concept if i["keyword"] == "cell type evolution")
    assert added["source"] == "paper_page" and added["weight"] == 1
    assert added["locked"] is False


def test_no_inbox_exits_cleanly(tmp_path):
    """收件箱不存在/无新文件时正常退出。"""
    args, config, inbox = _setup(tmp_path)
    args.inbox = str(tmp_path / "nonexistent")
    assert apr.run(args) == 0


def test_split_keywords():
    assert apr.split_keywords("CRISPR 递送， 单细胞测序\nCRISPR 递送;；scRNA-seq") == \
        ["CRISPR 递送", "单细胞测序", "scRNA-seq"]
    assert apr.split_keywords("") == []
