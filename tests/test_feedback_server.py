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
