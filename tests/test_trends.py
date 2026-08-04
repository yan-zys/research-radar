"""ranking/trends.py 聚合与写文件测试（无网络/AI，summarize_fn 注入假函数）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db  # noqa: E402
from ranking import trends  # noqa: E402


def _seed_db(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract) VALUES (?, ?, ?)",
        [("p:1", "Cross-species mapping with SAMap", "SAMap comparison"),
         ("p:2", "Brain evolution with SAMap", "brain evolution comparison"),
         ("p:old", "Ancient paper SAMap", "brain evolution")],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-20", "p:1", 9.0, "Must Read"),
         ("2026-07-22", "p:2", 8.0, "Important"),
         ("2026-05-01", "p:old", 9.0, "Must Read")],  # 超出 30 天窗口
    )
    conn.commit()
    return conn


def test_collect_stats(tmp_path):
    conn = _seed_db(tmp_path)
    stats = trends.collect_stats(conn, days=30, end_date="2026-07-23")
    conn.close()
    assert stats["total"] == 2  # p:old 被窗口排除
    assert stats["grade_counts"] == {"Must Read": 1, "Important": 1}
    assert stats["group_counts"]["方法"] == 2  # 两篇都命中 SAMap（methods 组）
    assert stats["group_counts"]["核心"] == 1  # p:2 命中 brain evolution（core 组）
    kws = dict(stats["top_keywords"])
    assert kws.get("SAMap") == 2


def test_run_writes_summary_with_fake_ai(tmp_path):
    conn = _seed_db(tmp_path)
    out = tmp_path / "trend_summary.txt"
    stats = trends.run(conn, days=30,
                       summarize_fn=lambda s: f"假总结：共 {s['total']} 篇",
                       out_file=out)
    conn.close()
    assert stats["total"] == 2
    assert out.read_text(encoding="utf-8").strip() == "假总结：共 2 篇"


def test_run_empty_window_skips_ai(tmp_path):
    conn = get_conn(tmp_path / "empty.db")
    init_db(conn)
    called = []
    stats = trends.run(conn, days=30,
                       summarize_fn=lambda s: called.append(1) or "x",
                       out_file=tmp_path / "trend_summary.txt")
    conn.close()
    assert stats["total"] == 0
    assert not called  # 无推荐时不调 AI
    assert not (tmp_path / "trend_summary.txt").exists()
