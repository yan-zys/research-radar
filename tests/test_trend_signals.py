"""外部趋势信号（trend_signals）纯函数与本地计数测试（无网络）。"""
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db  # noqa: E402
from ranking.trend_signals import (PubMedCounter, build_trend_ctx,  # noqa: E402
                                   combined_count_fn, corpus_count_fn, heat_of,
                                   salient_terms, title_terms, trend_values)


def test_title_terms_stopwords_dedup_max():
    # ≥5 字符 token、小写、去停用词（reveals/using）、保序去重、最多 5 个
    terms = title_terms(
        "SAMap reveals evolution of Drosophila brain using spatial transcriptomics "
        "evolution SAMap")
    assert terms == ["samap", "evolution", "drosophila", "brain", "spatial"]
    # 短词（<5 字符）与全停用词标题
    assert title_terms("The role of cell and gene in mice") == []
    assert title_terms("") == []


def test_salient_terms_merges_hits_and_title():
    match = {"hits": [{"keyword": "SAMap"}, {"keyword": "evolution"},
                      {"keyword": "samap"}]}  # 小写重复命中应被跳过
    terms = salient_terms(match, "SAMap evolution in Drosophila brain")
    assert terms == ["SAMap", "evolution", "drosophila", "brain"]


def test_heat_of_skips_none_and_zero():
    counts = {"a": 10, "b": 0, "c": None}
    heat = heat_of(["a", "b", "c"], lambda t: counts.get(t))
    assert heat == math.log1p(10)


def test_trend_values_normalization_and_top_journal_bonus():
    heats = {"p1": 10.0, "p2": 5.0, "p3": 0.0}
    is_tj = {"p1": True, "p3": True}
    tv = trend_values(heats, is_tj)
    assert tv["p1"] == 10.0             # 8 热度满分 + 2 顶刊
    assert tv["p2"] == 4.0              # 5/10*8，非顶刊无加分
    assert tv["p3"] == 2.0              # 热度 0 但顶刊 +2
    # 全零池：全为 0（即使有顶刊也不加分）
    assert trend_values({"p1": 0.0, "p2": 0.0}, {"p1": True}) == {"p1": 0.0, "p2": 0.0}


def test_combined_count_fn_none_fallback():
    primary = lambda t: {"x": None, "y": 5}.get(t)  # noqa: E731
    fallback = lambda t: 7  # noqa: E731
    count = combined_count_fn(primary, fallback)
    assert count("x") == 7   # primary 失败（None）→ 回退本地
    assert count("y") == 5   # primary 成功直接用
    assert callable(count.flush) and count.flush() is None  # flush 透传（无 flush 时为空操作）


def _seed_papers(conn):
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal, date) "
        "VALUES (?, ?, ?, ?, ?)",
        [("p1", "alpha cells", "", "J", "2026-07-20"),          # 窗口内，标题命中
         ("p2", "nothing here", "alpha abstract", "J", "2026-06-24"),  # 窗口边界，摘要命中
         ("p3", "alpha old", "", "J", "2026-06-23"),            # 窗口外
         ("p4", "unrelated", "", "J", "2026-07-10")],
    )
    conn.commit()


def test_corpus_count_fn_window_and_memoization(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed_papers(conn)
    count = corpus_count_fn(conn, date(2026, 7, 24))
    assert count("alpha") == 2      # 窗口 [2026-06-24, 2026-07-24]，标题/摘要 LIKE
    assert count("missing") == 0
    # 记忆化：删行后再查同词仍返回缓存值；新词才重新查询
    conn.execute("DELETE FROM papers")
    conn.commit()
    assert count("alpha") == 2
    assert count("beta") == 0
    conn.close()


def test_build_trend_ctx_offline_uses_corpus(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    _seed_papers(conn)
    ctx = build_trend_ctx(conn, "2026-07-24", offline=True)
    assert ctx["count_fn"]("alpha") == 2
    assert "nature" in ctx["tj_names"]  # 顶刊清单小写 NLM 名
    conn.close()


def test_pubmed_counter_cache_hit_no_network(tmp_path):
    """缓存命中（当日）直接返回计数，不发请求；flush 落盘后可再读。"""
    cache = tmp_path / "heat.json"
    cache.write_text('{"alpha": {"date": "2026-07-24", "count": 42}}', encoding="utf-8")
    c = PubMedCounter(date(2026, 7, 24), cache_path=cache, interval=0)
    assert c("alpha") == 42
    assert c.dirty is False
    # 手动置入新计数并 flush，文件内容更新
    c.cache["beta"] = {"date": "2026-07-24", "count": 7}
    c.dirty = True
    c.flush()
    c2 = PubMedCounter(date(2026, 7, 24), cache_path=cache, interval=0)
    assert c2("beta") == 7
