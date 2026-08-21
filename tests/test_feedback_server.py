"""feedback/server.py 的写入与去重逻辑测试（不启动网络服务）。"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location("feedback_server", ROOT / "feedback" / "server.py")
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def test_append_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "FEEDBACK_DIR", tmp_path)
    path = srv.append_feedback("biorxiv:10.1", "good", day="2026-07-23")
    srv.append_feedback("biorxiv:10.1", "good", day="2026-07-23")   # 重复，不应再写
    srv.append_feedback("biorxiv:10.1", "bad", day="2026-07-23")    # 不同 rating，应写入
    srv.append_feedback("pubmed:10.2", "ok", day="2026-07-23")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == [
        {"paper_id": "biorxiv:10.1", "rating": "good"},
        {"paper_id": "biorxiv:10.1", "rating": "bad"},
        {"paper_id": "pubmed:10.2", "rating": "ok"},
    ]
    assert path.name == "2026-07-23.jsonl"


def test_new_ratings_accepted_and_deduped(tmp_path, monkeypatch):
    """read / star 新 rating 与白名单一致，append 层照常写入去重。"""
    monkeypatch.setattr(srv, "FEEDBACK_DIR", tmp_path)
    assert set(srv.RATINGS) == {"good", "ok", "bad", "read", "star"}
    path = srv.append_feedback("biorxiv:10.1", "star", day="2026-07-23")
    srv.append_feedback("biorxiv:10.1", "star", day="2026-07-23")  # 重复，不应再写
    srv.append_feedback("biorxiv:10.1", "read", day="2026-07-23")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == [
        {"paper_id": "biorxiv:10.1", "rating": "star"},
        {"paper_id": "biorxiv:10.1", "rating": "read"},
    ]


def test_feedback_with_reason_updates_in_place(tmp_path, monkeypatch):
    """bad 重复提交带 reason：不新增行，原地更新原记录的 reason。"""
    monkeypatch.setattr(srv, "FEEDBACK_DIR", tmp_path)
    path = srv.append_feedback("biorxiv:10.1", "bad", day="2026-08-21")
    srv.append_feedback("biorxiv:10.1", "bad", reason="方向不符", day="2026-08-21")
    srv.append_feedback("biorxiv:10.1", "bad", reason="物种不符", day="2026-08-21")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == [{"paper_id": "biorxiv:10.1", "rating": "bad", "reason": "物种不符"}]


def test_append_request_writes_inbox(tmp_path, monkeypatch):
    """关键词/文献请求追加到收件箱 jsonl。"""
    monkeypatch.setattr(srv, "REQUESTS_DIR", tmp_path)
    path = srv.append_request({"type": "keywords", "text": "CRISPR 递送"}, day="2026-08-21")
    srv.append_request({"type": "papers", "ids": ["10.1/abc", "40123456"]}, day="2026-08-21")
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines == [
        {"type": "keywords", "text": "CRISPR 递送"},
        {"type": "papers", "ids": ["10.1/abc", "40123456"]},
    ]
    assert path.name == "2026-08-21.jsonl"


def test_split_paper_ids_separators_and_cap():
    """DOI/PMID 拆分：中英文逗号/分号/换行分隔，超过 10 个截断。"""
    assert srv.split_paper_ids("10.1/abc，40123456\n 10.2/def;10.3/ghi") == \
        ["10.1/abc", "40123456", "10.2/def", "10.3/ghi"]
    many = " ".join(f"400000{i:02d}" for i in range(15))
    assert len(srv.split_paper_ids(many)) == srv.MAX_PAPER_IDS
    assert srv.split_paper_ids("") == []
