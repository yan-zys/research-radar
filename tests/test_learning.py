"""feedback/learning 的测试（monkeypatch AI，禁止真实网络调用）。"""
import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db  # noqa: E402
from feedback import learning  # noqa: E402

HEADER = """# 关键词库（唯一被 filter/scoring 读取的配置）
# 二级权重规则（用户确认）：locked 种子词不得降权
"""

CONFIG = {
    "keywords": {
        "species": {"weight": 5, "items": [
            {"keyword": "tardigrade", "source": "seed", "weight": 2, "locked": True},
            {"keyword": "rotifer", "source": "ai", "weight": 2},
            {"keyword": "annelid", "source": "paper:x", "weight": 1},
        ]},
        "concept": {"weight": 10, "items": [
            {"keyword": "brain evolution", "source": "seed", "weight": 2, "locked": True},
        ]},
    },
    "negative": [],
}

FAKE_AI_RESULT = {
    "level_A_core_monitor": ["onychophoran brain"],
    "level_B_method_transfer": [],
    "level_C_trend_tracking": ["tardigrade", "archive word"],  # 已有词条/已拒绝词，应被去重
    "reasoning": {"onychophoran brain": "用户认可论文中的核心主题"},
}


def make_env(tmp_path) -> argparse.Namespace:
    """搭建临时环境：config / db / 反馈目录 / 候选文件 / processed.log。"""
    config_path = tmp_path / "keyword_config.yaml"
    config_path.write_text(HEADER + yaml.dump(CONFIG, allow_unicode=True, sort_keys=False),
                           encoding="utf-8")
    db_path = tmp_path / "papers.db"
    conn = get_conn(db_path)
    init_db(conn)
    conn.execute(
        "INSERT INTO papers (paper_id, title, abstract) VALUES (?, ?, ?)",
        ("pubmed:1", "tardigrade rotifer annelid brain evolution", "some abstract"),
    )
    conn.commit()
    conn.close()
    feedback_dir = tmp_path / "feedback_in"
    feedback_dir.mkdir()
    return argparse.Namespace(
        date="2026-07-22", all=False,
        feedback_dir=str(feedback_dir),
        config=str(config_path),
        candidates=str(tmp_path / "output_candidates.yaml"),
        archive=str(tmp_path / "archive_rejected.yaml"),
        processed_log=str(tmp_path / "processed.log"),
        db=str(db_path), max_new=5,
    )


def write_feedback(args, lines):
    p = Path(args.feedback_dir) / f"{args.date}.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def load_weights(args) -> dict:
    data = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    return {i["keyword"]: i.get("weight", 1)
            for g in data["keywords"].values() for i in g["items"]}


def forbid_ai(prompt, response_format="json"):
    raise AssertionError("不应调用 AI")


def test_no_feedback_file_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "call_model", forbid_ai)
    args = make_env(tmp_path)
    assert learning.run(args) == 0
    assert not Path(args.processed_log).exists()


def test_bad_feedback_downgrades_weights(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "call_model", forbid_ai)  # 无 good 反馈不调 AI
    args = make_env(tmp_path)
    write_feedback(args, ['{"paper_id": "pubmed:1", "rating": "bad", "date": "2026-07-22"}'])
    assert learning.run(args) == 0
    w = load_weights(args)
    assert w["tardigrade"] == 2       # locked 种子词：无论负反馈如何不得降权
    assert w["brain evolution"] == 2  # locked 种子词不降
    assert w["rotifer"] == 1          # weight 2 但非 locked：不是真种子，降到 1
    assert w["annelid"] == 1          # weight 1 已到下限，不再降
    text = Path(args.config).read_text(encoding="utf-8")
    assert text.startswith("# 关键词库")  # 头部注释保留


def test_good_feedback_mines_pending_candidates(tmp_path, monkeypatch):
    captured = {}

    def fake_call_model(prompt, response_format="json"):
        captured["called"] = True
        return FAKE_AI_RESULT

    monkeypatch.setattr(learning, "call_model", fake_call_model)
    args = make_env(tmp_path)
    Path(args.archive).write_text(
        yaml.dump([{"keyword": "archive word"}], allow_unicode=True), encoding="utf-8")
    write_feedback(args, ['{"paper_id": "pubmed:1", "rating": "good", "date": "2026-07-22"}'])
    assert learning.run(args) == 0
    assert captured.get("called")
    cands = yaml.safe_load(Path(args.candidates).read_text(encoding="utf-8"))
    assert len(cands) == 1  # tardigrade（已在词库）与 archive word（已拒绝）被去重
    c = cands[0]
    assert c["keyword"] == "onychophoran brain"
    assert c["status"] == "pending"   # 必须人工审核，绝不自动 merge
    assert c["source"] == "feedback"
    assert c["level"] == "A"
    assert c["reason"]
    assert load_weights(args)["tardigrade"] == 2  # good 反馈不改权重


def test_run_is_idempotent(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_call_model(prompt, response_format="json"):
        calls["n"] += 1
        return FAKE_AI_RESULT

    monkeypatch.setattr(learning, "call_model", fake_call_model)
    args = make_env(tmp_path)
    write_feedback(args, [
        '{"paper_id": "pubmed:1", "rating": "bad", "date": "2026-07-22"}',
        '{"paper_id": "pubmed:1", "rating": "good", "date": "2026-07-22"}',
    ])
    assert learning.run(args) == 0
    w1 = load_weights(args)
    c1 = yaml.safe_load(Path(args.candidates).read_text(encoding="utf-8"))
    assert w1["rotifer"] == 1
    # 第二次运行：反馈文件已在 processed.log 中，整体跳过
    assert learning.run(args) == 0
    assert load_weights(args) == w1
    assert yaml.safe_load(Path(args.candidates).read_text(encoding="utf-8")) == c1
    assert calls["n"] == 1


def test_star_treated_as_positive_and_read_skipped(tmp_path, monkeypatch):
    """star（收藏）视为正反馈参与挖掘；read（已读）与未知 rating 跳过且不让流程崩溃。"""
    captured = {}

    def fake_call_model(prompt, response_format="json"):
        captured["called"] = True
        return FAKE_AI_RESULT

    monkeypatch.setattr(learning, "call_model", fake_call_model)
    args = make_env(tmp_path)
    Path(args.archive).write_text(
        yaml.dump([{"keyword": "archive word"}], allow_unicode=True), encoding="utf-8")
    write_feedback(args, [
        '{"paper_id": "pubmed:1", "rating": "star", "date": "2026-07-22"}',
        '{"paper_id": "pubmed:1", "rating": "read", "date": "2026-07-22"}',
        '{"paper_id": "pubmed:1", "rating": "whatever", "date": "2026-07-22"}',
    ])
    assert learning.run(args) == 0
    assert captured.get("called")  # star 触发候选词挖掘
    cands = yaml.safe_load(Path(args.candidates).read_text(encoding="utf-8"))
    assert [c["keyword"] for c in cands] == ["onychophoran brain"]
    assert load_weights(args)["rotifer"] == 2  # read/未知 rating 不触发降权


def test_read_only_feedback_does_not_call_ai(tmp_path, monkeypatch):
    """只有 read 反馈时：不降权、不调用 AI、正常退出。"""
    monkeypatch.setattr(learning, "call_model", forbid_ai)
    args = make_env(tmp_path)
    write_feedback(args, ['{"paper_id": "pubmed:1", "rating": "read", "date": "2026-07-22"}'])
    assert learning.run(args) == 0
    assert load_weights(args)["rotifer"] == 2
    assert not Path(args.candidates).exists()
