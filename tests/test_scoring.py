"""scoring 的 grade 分档、期刊分档与加权计算测试（无网络/AI）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import get_conn, init_db  # noqa: E402
from ranking.scoring import compute_scores, grade_of, is_atlas_candidate, journal_score, run  # noqa: E402

WEIGHTS = {"research_relevance": 0.5, "ai_semantic_relevance": 0.2,
           "journal_influence": 0.2, "trend_value": 0.1}

KW = {"keywords": {
        "methods": {"weight": 8, "items": [{"keyword": "scRNA-seq", "weight": 1}]},
        "tools": {"weight": 9, "items": [{"keyword": "SAMap", "weight": 2}]},
        "concept": {"weight": 10, "items": [{"keyword": "evolution", "weight": 1}]}},
      "negative": []}


def test_grade_boundaries():
    # 纯总分定级（2026-08-05 用户确认）：≥7 Must Read / ≥5 Important / 其余 Relate
    assert grade_of(7.0) == "Must Read"
    assert grade_of(6.99) == "Important"
    assert grade_of(5.0) == "Important"
    assert grade_of(4.99) == "Relate"
    assert grade_of(0.0) == "Relate"  # 低分也入选，等级为 Relate


def test_journal_tiers():
    assert journal_score("Nature Methods") == 10.0
    assert journal_score("Science Advances") == 10.0
    assert journal_score("Cell") == 10.0
    assert journal_score("eLife") == 7.0
    assert journal_score("PLOS Biology") == 7.0
    assert journal_score("bioRxiv") == 5.0
    assert journal_score("Some Obscure Journal") == 5.0


def test_weighted_total_and_normalization():
    import math
    rows = [
        {"paper_id": "x:1", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 8},
        {"paper_id": "x:2", "title": "evolution", "abstract": "",
         "journal": "bioRxiv", "rule_score": 50, "ai_score": None},
    ]
    # 外部趋势信号（注入计数，无网络）：x:1 命中三词，x:2 只命中 evolution
    counts = {"scrna-seq": 50, "samap": 100, "evolution": 10}
    trend_ctx = {"count_fn": lambda t: counts.get(t.lower(), 0),
                 "tj_names": {"nature"}}
    scored = {e["paper_id"]: e for e in compute_scores(rows, WEIGHTS, KW,
                                                       trend_ctx=trend_ctx)}
    a, b = scored["x:1"], scored["x:2"]
    heat_a = math.log1p(50) + math.log1p(100) + math.log1p(10)
    heat_b = math.log1p(10)
    trend_a = 8.0 + 2.0               # 热度池内最大归一化 8；Nature 顶刊当期 +2
    trend_b = heat_b / heat_a * 8.0   # bioRxiv 非顶刊，无加分
    # a：rule 归一化 10；ai=8；期刊 10
    assert a["total_score"] == round(10 * 0.5 + 8 * 0.2 + 10 * 0.2 + trend_a * 0.1, 2)
    assert a["grade"] == "Must Read"
    # b：rule 归一化 50/100*10=5；ai 缺失按 5；bioRxiv=5（2026-08-05 预印本降回 5 分）
    assert b["total_score"] == round(5 * 0.5 + 5 * 0.2 + 5 * 0.2 + trend_b * 0.1, 2)
    assert b["grade"] == "Relate"  # 预印本降分后 total<5 → Relate


def test_compute_scores_without_trend_ctx_trend_zero():
    """trend_ctx 缺省时 trend_value 按 0 计（离线纯计算）。"""
    rows = [{"paper_id": "x:1", "title": "SAMap evolution scRNA-seq", "abstract": "",
             "journal": "Nature", "rule_score": 100, "ai_score": 8}]
    [e] = compute_scores(rows, WEIGHTS, KW)
    assert e["total_score"] == round(10 * 0.5 + 8 * 0.2 + 10 * 0.2 + 0 * 0.1, 2)


def test_top15_all_selected_no_score_cutoff(tmp_path):
    """低分论文也进入 Top 推荐（不再因 <5 剔除），grade 为 Relate。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("x:high", "SAMap evolution scRNA-seq", "", "Nature"),
         ("x:low", "evolution", "", "bioRxiv")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:high", 100, 1, 9), ("x:low", 1, 1, 3)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    ids = {e["paper_id"]: e["grade"] for e in top}
    assert ids["x:high"] == "Must Read"
    assert ids["x:low"] == "Relate"  # 低分不剔除
    grades = {r[0] for r in conn.execute(
        "SELECT grade FROM recommendations WHERE date = '2026-07-24'")}
    assert grades == {"Must Read", "Relate"}
    conn.close()


def _seed_run_db(tmp_path):
    """三篇通过过滤的论文；x:recent 13 天前推荐过（90 天窗口内），
    x:old 是 100 天前推荐的（超出 90 天 D 层窗口）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("x:new", "SAMap evolution scRNA-seq", "", "Nature"),
         ("x:recent", "SAMap evolution scRNA-seq recent", "", "Nature"),
         ("x:old", "SAMap evolution scRNA-seq old", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:new", 100, 1, 9), ("x:recent", 100, 1, 9), ("x:old", 100, 1, 9)],
    )
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [("2026-07-10", "x:recent", 9.5, "Must Read"),   # run_date 前 13 天，双排除
         ("2026-04-14", "x:old", 9.5, "Must Read")],      # run_date 前 100 天，仅 D 层回补
    )
    conn.commit()
    return conn


def test_ai_veto_excludes_low_relevance():
    """ai_score 已评且 <3 的论文被 AI 否决，不进入推荐；未评分（None）不受影响。"""
    rows = [
        {"paper_id": "x:ok", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 8},
        {"paper_id": "x:veto", "title": "SAMap evolution scRNA-seq", "abstract": "",
         "journal": "Nature", "rule_score": 100, "ai_score": 0},
    ]
    scored = {e["paper_id"]: e for e in compute_scores(rows, WEIGHTS, KW)}
    assert "x:ok" in scored
    assert "x:veto" not in scored


def test_run_pub_date_filters_candidates(tmp_path):
    """pub_date 限定候选论文的发表日期（papers.date）：补算某日发表文献的
    历史日报时，只从该日发表的论文中选取；默认 None 不过滤。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal, date) "
        "VALUES (?, ?, ?, ?, ?)",
        [("x:0722", "SAMap evolution scRNA-seq a", "", "Nature", "2026-07-22"),
         ("x:0723", "SAMap evolution scRNA-seq b", "", "Nature", "2026-07-23")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("x:0722", 100, 1, 9), ("x:0723", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-22", pub_date="2026-07-22",
              offline=True)
    assert [e["paper_id"] for e in top] == ["x:0722"]
    # 默认不带 pub_date：x:0722 已在 2026-07-22 进过 recommendations，全历史去重
    # 排除出 A-C 层，且 2 天前 < 90 天 D 层窗口 → 两个通道都排除，只剩 x:0723
    top_all = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    assert {e["paper_id"] for e in top_all} == {"x:0723"}
    conn.close()


def test_run_dedupes_history_with_90d_fallback(tmp_path):
    """A-C 层全历史去重：x:recent（13 天前推过）与 x:old（100 天前推过）都进不了
    A-C；x:recent 还在 90 天窗口内连 D 层也进不了，x:old 只能由 D 兜底层回补。"""
    conn = _seed_run_db(tmp_path)
    top = run(conn, WEIGHTS, KW, run_date="2026-07-23", offline=True)
    by_id = {e["paper_id"]: e for e in top}
    assert [e["paper_id"] for e in top] == ["x:new", "x:old"]
    assert by_id["x:new"]["tier"] == "A"
    assert by_id["x:old"]["tier"] == "D"          # 超出 90 天窗口，仅 D 层回补
    assert "x:recent" not in by_id                # 13 天前推过：全历史 + 90 天双排除
    conn.close()


def test_tiered_fallback_fills_top15(tmp_path):
    """A 层只有 2 篇时，B/C 层补足 15 篇：tier 标记正确且 A 层排在最前。
    B 层只收顶刊清单内且已评 AI 的论文；C 层收被否决的关键词/顶刊论文。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    papers = [("a:1", "SAMap evolution scRNA-seq one", "", "Nature"),
              ("a:2", "SAMap evolution scRNA-seq two", "", "eLife")]
    papers += [(f"b:{i}", f"unrelated topic {i}", "", "Nature communications")
               for i in range(8)]
    papers += [(f"c:{i}", f"vetoed topic {i}", "", "Nature") for i in range(5)]
    papers += [("n:1", "noise not top journal", "", "Some Obscure Journal")]
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        papers,
    )
    scores = [("a:1", 100, 1, 9), ("a:2", 80, 1, 8)]
    scores += [(f"b:{i}", 0, 0, 5) for i in range(8)]   # 顶刊、已评 AI、未否决
    scores += [(f"c:{i}", 50, 1, 1) for i in range(5)]  # 被 AI 否决
    scores += [("n:1", 99, 0, None)]                    # 非顶刊+规则未命中 → 不推荐
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        scores,
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    assert len(top) == 15
    assert "n:1" not in {e["paper_id"] for e in top}
    tiers = [e["tier"] for e in top]
    assert tiers == ["A", "A"] + ["B"] * 8 + ["C"] * 5
    assert {e["paper_id"] for e in top[:2]} == {"a:1", "a:2"}
    # 层内按 total_score 降序
    for t in ("A", "B", "C"):
        layer = [e["total_score"] for e in top if e["tier"] == t]
        assert layer == sorted(layer, reverse=True)


def test_tiered_fallback_excludes_negative(tmp_path):
    """梯队兜底（B/C 层）也要剔除命中 negative 排除词的论文
    （医学/临床噪声，含此类词汇的邮件易触发 SMTP 550 内容过滤）。"""
    kw = {"keywords": KW["keywords"], "negative": ["cancer"]}
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    papers = [("a:1", "SAMap evolution scRNA-seq one", "", "Nature"),
              ("b:ok", "unrelated topic", "", "Nature communications"),
              ("b:neg", "unrelated cancer topic", "", "Nature communications"),
              ("c:neg", "vetoed cancer topic", "", "Nature")]
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        papers,
    )
    scores = [("a:1", 100, 1, 9), ("b:ok", 0, 0, 6),
              ("b:neg", 99, 0, 6), ("c:neg", 99, 1, 1)]
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        scores,
    )
    conn.commit()
    top = run(conn, WEIGHTS, kw, run_date="2026-07-24", offline=True)
    conn.close()
    ids = [e["paper_id"] for e in top]
    assert ids == ["a:1", "b:ok"]  # negative 命中的 b:neg / c:neg 均被剔除，不兜底


KW_CORE = {"keywords": {
    "core": {"weight": 15, "items": [{"keyword": "brain evolution", "weight": 2}]},
    "core_broad": {"weight": 15, "items": [{"keyword": "cerebellum", "weight": 2}]},
    "tools": {"weight": 9, "items": [{"keyword": "SAMap", "weight": 2}]}},
  "negative": []}


def test_core_hit_forced_first_and_bypasses_veto(tmp_path):
    """core 组（脑/神经核心方向）命中的论文进 A0 层：排在最前且绕过 AI 否决
    （用户要求"脑/神经相关命中一定要推送"）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("core:1", "SAMap comparative study",
          "regulatory innovation in brain evolution", "Nature communications"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("core:1", 30, 1, 0),   # AI 否决（<3）但 core 命中 → 仍必推送
         ("a:1", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW_CORE, run_date="2026-07-24", offline=True)
    conn.close()
    assert top[0]["paper_id"] == "core:1"
    assert top[0]["tier"] == "A0"
    assert {e["paper_id"] for e in top} == {"core:1", "a:1"}


def test_core_hit_negative_still_excluded(tmp_path):
    """core 命中但同中 negative 排除词的论文仍被剔除（医学噪声规则优先于必推送）。"""
    kw = {"keywords": KW_CORE["keywords"], "negative": ["cancer"]}
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("core:neg", "brain evolution cancer study", "", "Nature"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("core:neg", 30, 1, 9), ("a:1", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, kw, run_date="2026-07-24", offline=True)
    conn.close()
    assert [e["paper_id"] for e in top] == ["a:1"]


def test_core_broad_requires_omics_and_evolution_anchors(tmp_path):
    """core_broad 泛词（cerebellum 等）必须与组学锚点 + 演化锚点双共现才进 A0：
    双共现者即使被 AI 否决也必推送；只有组学锚点、无演化视角的论文不进 A0
    （否决后落 C 层，排在未否决的 A 层之后）——2026-08-04 用户确认：
    纯神经机制论文（无演化/比较视角）即使做单细胞也不强推。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("broad:evo", "Cerebellum development in primates",
          "single-cell transcriptomics reveals the evolution of the primate cerebellum",
          "Nature communications"),
         ("broad:noevo", "Cerebellum development in primates",
          "single-cell transcriptomics of the primate cerebellum",
          "Nature communications"),
         ("broad:clin", "Cerebellum cohort study",
          "behavioral scores in a clinical cohort", "Nature communications"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("broad:evo", 30, 1, 0),    # 否决但 cerebellum+组学+演化 双共现 → A0
         ("broad:noevo", 30, 1, 1),  # 否决、有组学锚点但无演化锚点 → 非 A0，落 C 层
         ("broad:clin", 30, 1, 1),   # 否决且双锚点皆无 → 落 C 层
         ("a:1", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW_CORE, run_date="2026-07-24", offline=True)
    conn.close()
    tiers = {e["paper_id"]: e["tier"] for e in top}
    assert tiers["broad:evo"] == "A0"
    assert tiers["broad:noevo"] == "C"
    assert tiers["broad:clin"] == "C"
    assert top[0]["paper_id"] == "broad:evo"  # A0 层在最前
    assert [e["paper_id"] for e in top].index("broad:noevo") > \
        [e["paper_id"] for e in top].index("a:1")  # C 层排在 A 层之后


def test_atlas_forced_must_read_and_a0(tmp_path):
    """AI 分区 section=="atlas"（新动物单细胞图谱）的论文：进 A0 层排最前、
    绕过 AI 否决、grade 强制 Must Read（2026-10-13 用户要求：图谱权重最高）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("atlas:1", "A single-cell atlas of the octopus brain", "", "bioRxiv"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, section) "
        "VALUES (?, ?, ?, ?, ?)",
        [("atlas:1", 5, 1, 1, "atlas"),   # 低规则分 + AI 否决，仍必推送且必读
         ("a:1", 100, 1, 9, "research")],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    by_id = {e["paper_id"]: e for e in top}
    assert by_id["atlas:1"]["grade"] == "Must Read"
    assert by_id["atlas:1"]["tier"] == "A0"
    assert top[0]["paper_id"] == "atlas:1"
    assert by_id["a:1"]["grade"] == "Must Read"  # 高分对照不受影响


def test_is_atlas_candidate():
    """图谱标题启发式：atlas + 单细胞锚点 + 领域信号（神经/演化/非模式动物），
    且标题无排除信号（植物/微生物/计算方法/人类疾病）。"""
    # 神经图谱（线虫 L1：标题无物种，靠 "nervous" 领域信号）
    assert is_atlas_candidate(
        "A gene expression atlas of a juvenile nervous system",
        "a gene expression atlas of a juvenile nervous system single-cell rna sequencing")
    # 跨物种演化框架（Science 心脏宏观演化图谱：非神经也强制）
    assert is_atlas_candidate(
        "Spatially resolved single-cell atlas reveals the macroevolutionary "
        "trajectory of animal hearts",
        "spatially resolved single-cell atlas macroevolutionary trajectory single-cell")
    # 非模式动物图谱
    assert is_atlas_candidate(
        "A single-cell atlas of the octopus brain",
        "a single-cell atlas of the octopus brain")
    # 无单细胞锚点（磷酸化图谱）不判
    assert not is_atlas_candidate(
        "A phosphorylation atlas of kinase signaling",
        "a phosphorylation atlas of kinase signaling mass spectrometry biochemistry")
    # 标题无 atlas 不判（即使有锚点）
    assert not is_atlas_candidate(
        "Single-cell analysis of the mouse brain",
        "single-cell analysis of the mouse brain single-cell transcriptomics")
    # 无领域信号（模式动物普通组织图谱）不强制，走正常评分；
    # 摘要里的 evolution/comparative 不算数——领域信号只认标题
    assert not is_atlas_candidate(
        "Single-cell atlas of the mouse ovary reveals molecular drivers",
        "single-cell atlas of the mouse ovary reveals the evolution of follicles")
    assert not is_atlas_candidate(
        "Single-Cell Atlas of Human Temporomandibular Joint Condyle",
        "single-cell atlas human temporomandibular joint comparative analysis")
    # 植物图谱标题排除（即使有锚点与领域词）
    assert not is_atlas_candidate(
        "A comprehensive Arabidopsis transcription factor binding atlas",
        "arabidopsis single-cell evolution comparative")
    # 计算方法论文标题排除
    assert not is_atlas_candidate(
        "AtlasOT - The Fused Unbalanced Gromov-Wasserstein for Multimodal Data",
        "optimal transport single-cell evolution")
    # 人类疾病图谱标题排除（即使摘要含神经词）
    assert not is_atlas_candidate(
        "Single-cell atlas of aqueous humor outflow in glaucomatous eyes",
        "glaucomatous single-cell brain neural")


def test_atlas_title_heuristic_forces_must_read(tmp_path):
    """图谱论文未过 AI 分区（section 为 NULL）时，标题启发式仍强制 Must Read 并进
    A0——2026-10-14 线虫 L1 神经图谱仅凭关键词分 4.59 被埋成 Relate 的回归测试。
    对照：无组学锚点的 atlas 论文（磷酸化图谱）不受影响，仍按总分定级。"""
    rows = [
        {"paper_id": "atlas:worm",
         "title": "A gene expression atlas of a juvenile nervous system",
         "abstract": "single-cell RNA sequencing of C. elegans L1 larvae",
         "journal": "bioRxiv", "rule_score": 0, "ai_score": None},
        {"paper_id": "atlas:phos", "title": "A phosphorylation atlas of kinase signaling",
         "abstract": "mass spectrometry biochemistry",
         "journal": "bioRxiv", "rule_score": 0, "ai_score": None},
    ]
    scored = {e["paper_id"]: e for e in compute_scores(rows, WEIGHTS, KW)}
    # 两篇总分相同：rule 0 + ai 默认 5×0.2 + bioRxiv 5×0.2 = 2.0 → 本应 Relate
    assert scored["atlas:phos"]["total_score"] == 2.0
    assert scored["atlas:phos"]["grade"] == "Relate"
    assert scored["atlas:worm"]["grade"] == "Must Read"  # 启发式强制，非总分够线

    # run() 层面：section=NULL + AI 否决的图谱论文仍进 A0 排最前
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("atlas:worm", "A gene expression atlas of a juvenile nervous system",
          "single-cell RNA sequencing of C. elegans L1 larvae", "bioRxiv"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score) "
        "VALUES (?, ?, ?, ?)",
        [("atlas:worm", 5, 1, 1),   # AI 否决 + 无 section，仍须 A0 + Must Read
         ("a:1", 100, 1, 9)],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    by_id = {e["paper_id"]: e for e in top}
    assert by_id["atlas:worm"]["tier"] == "A0"
    assert by_id["atlas:worm"]["grade"] == "Must Read"
    assert top[0]["paper_id"] == "atlas:worm"


def test_off_scope_excluded_from_recommendations(tmp_path):
    """AI 分区 section=="off_scope" 的论文不推荐、不占 Top N（2026-10-15 用户要求：
    日报只保留图谱/研究/计算三个分区，无"其他"）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("off:1", "Eye-tracking study of atlas cover design", "", "PloS one"),
         ("off:2", "Federated learning for lesion detection", "", "PloS one"),
         ("a:1", "SAMap evolution scRNA-seq", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, section) "
        "VALUES (?, ?, ?, ?, ?)",
        [("off:1", 100, 1, 9, "off_scope"),  # 规则/AI 双高分也被剔除
         ("off:2", 100, 1, 9, "other"),      # 旧 prompt 的历史标记同等剔除
         ("a:1", 100, 1, 9, "research")],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    ids = [e["paper_id"] for e in top]
    assert "off:1" not in ids
    assert "off:2" not in ids
    assert "a:1" in ids


def test_a_layer_requires_ai_score(tmp_path):
    """A 层必须已评 AI：ai_score 为 NULL 的关键词命中论文不推荐（2026-10-16：
    历史规则改动回捞的 1320 篇未评 AI 旧论文经 A 层漏入日报）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("unscored:1", "SAMap evolution scRNA-seq", "", "Nature"),
         ("scored:1", "SAMap evolution scRNA-seq 2", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, section) "
        "VALUES (?, ?, ?, ?, ?)",
        [("unscored:1", 100, 1, None, None),
         ("scored:1", 100, 1, 6, "research")],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    ids = [e["paper_id"] for e in top]
    assert "unscored:1" not in ids
    assert "scored:1" in ids


def test_off_scope_blocks_core_hit(tmp_path):
    """off_scope 优先于 core 关键词命中：AI 结合摘要判定无关后，词面命中不再强推
    （2026-10-16：鲸 Müller 胶质论文命中 glia 等 core_broad 词但 AI 判 off_scope）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("off:core", "brain evolution in clinical cohorts", "", "Nature")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, section) "
        "VALUES (?, ?, ?, ?, ?)",
        [("off:core", 100, 1, 9, "off_scope")],
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW_CORE, run_date="2026-07-24", offline=True)
    conn.close()
    assert "off:core" not in [e["paper_id"] for e in top]


def test_off_scope_does_not_block_a0(tmp_path):
    """atlas 启发式优先级高于 off_scope：图谱论文即使 section 异常也仍推送
    （图谱为最高优先级，AI 误标时由标题启发式兜底）。"""
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, journal) VALUES (?, ?, ?, ?)",
        [("atlas:1", "A single-cell atlas of the octopus brain", "", "bioRxiv")],
    )
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, section) "
        "VALUES (?, ?, ?, ?, ?)",
        [("atlas:1", 5, 1, 1, "off_scope")],  # 数据异常：atlas 标题被误标 off_scope
    )
    conn.commit()
    top = run(conn, WEIGHTS, KW, run_date="2026-07-24", offline=True)
    conn.close()
    by_id = {e["paper_id"]: e for e in top}
    assert by_id["atlas:1"]["tier"] == "A0"  # 启发式 atlas 命中仍进 A0
    assert by_id["atlas:1"]["grade"] == "Must Read"
