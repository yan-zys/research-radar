"""mustread_collection 年度合集测试（临时 DB，无网络/AI）。

email/ 目录与标准库 email 重名（无 __init__.py），须按文件路径加载模块。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mustread_collection", ROOT / "email" / "mustread_collection.py")
mc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mc)

TREND = {"overview": "测试趋势总览", "directions": ["方向一：测试"],
         "common_trend": "测试共同趋势", "value": "测试价值"}


def _seed_db(tmp_path):
    conn = get_conn(tmp_path / "t.db")
    init_db(conn)
    conn.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("pubmed:1", "Single-cell atlas of octopus brain", "We present ...",
          "Doe J", "Nature", "2026-07-22", "10.1", "https://pubmed.ncbi.nlm.nih.gov/1/"),
         ("pubmed:2", "Brain evolution in annelids", "Here we show ...",
          "Lee B", "Nature", "2026-07-22", "10.2", "https://pubmed.ncbi.nlm.nih.gov/2/"),
         ("pubmed:3", "Clinical trial of chemotherapy in cancer patients",
          "Randomized trial ...", "Wang C", "Nature", "2026-07-22", "10.3",
          "https://pubmed.ncbi.nlm.nih.gov/3/"),
         ("pubmed:4", "Soil texture classification with deep learning", "We classify ...",
          "Li D", "Some Journal", "2026-07-22", "10.4",
          "https://pubmed.ncbi.nlm.nih.gov/4/")],
    )
    # abstract_cn/title_cn 预先填好，避免测试触发真实 AI 调用
    conn.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, reason, "
        "title_cn, one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("pubmed:1", 50, 1, 9.0, "核心方向高度相关", "章鱼脑单细胞图谱",
          "为解决章鱼脑细胞类型不清问题，作者用单细胞转录组构建全脑图谱，发现深度同源细胞类型。",
          "章鱼脑神经元类型缺乏系统图谱。作者用单细胞转录组测序构建全脑图谱，发现深度同源。"),
         ("pubmed:2", 10, 0, 8.0, "顶刊高相关", "环节动物脑演化",
          "为解决两侧对称动物脑起源争议，作者比较环节动物脑转录组，发现保守神经发育模块。",
          "环节动物与脊椎动物脑的同源性长期存疑。作者比较多个环节动物物种的脑转录组。"),
         ("pubmed:3", 0, 0, 1.0, "医学临床噪声", "化疗临床试验",
          "临床肿瘤试验，与研究方向无关。", "随机对照临床试验摘要。"),
         ("pubmed:4", 0, 0, 1.0, "无关", "土壤分类", "与研究方向无关。", "土壤深度学习分类摘要。")],
    )
    conn.commit()
    return conn


def test_eligible_pool_filters(tmp_path):
    """候选池口径：negative 词剔除；passed_filter=1 或（顶刊且已评 AI）入选；其余排除。"""
    conn = _seed_db(tmp_path)
    kw_config = mc.load_kw_config()
    tj = {"nature"}
    pool, conn_of = mc.eligible_pool([conn], kw_config, tj)
    ids = {r["paper_id"] for r in pool}
    conn.close()
    assert "pubmed:1" in ids  # passed_filter=1
    assert "pubmed:2" in ids  # 顶刊 + 已评 AI
    assert "pubmed:3" not in ids  # negative（clinical/cancer）全层剔除
    assert "pubmed:4" not in ids  # 非顶刊且未过关键词过滤
    assert conn_of["pubmed:1"] is not None


def test_eligible_pool_merges_backfill_and_filters_year(tmp_path):
    """合并库：主库优先去重；year 过滤掉非目标年份论文。"""
    main = _seed_db(tmp_path)
    bf = get_conn(tmp_path / "bf.db")
    init_db(bf)
    bf.executemany(
        "INSERT INTO papers (paper_id, title, abstract, authors, journal, date, doi, url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("pubmed:1", "回填库重复的老标题", "dup", "X", "Nature", "2026-03-01", "d1", "u1"),
         ("pubmed:5", "Cnidarian neuron single-cell atlas", "abs", "Y", "Cell",
          "2026-02-10", "d2", "u2"),
         ("pubmed:6", "Old 2025 paper on brain evolution", "abs", "Z", "Nature",
          "2025-12-01", "d3", "u3")],
    )
    bf.executemany(
        "INSERT INTO paper_scores (paper_id, rule_score, passed_filter, ai_score, reason, "
        "title_cn, one_line_summary_cn, abstract_cn) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [("pubmed:1", 99, 1, 9.0, "r", "t", "o", "a"),
         ("pubmed:5", 40, 1, 9.0, "r", "刺胞动物神经元图谱", "o", "a"),
         ("pubmed:6", 30, 1, 9.0, "r", "2025 旧文", "o", "a")],
    )
    bf.commit()
    kw_config = mc.load_kw_config()
    pool, conn_of = mc.eligible_pool([main, bf], kw_config, {"nature", "cell"}, date_prefix="2026")
    by_id = {r["paper_id"]: r for r in pool}
    main.close()
    bf.close()
    assert by_id["pubmed:1"]["title"] == "Single-cell atlas of octopus brain"  # 主库优先
    assert "pubmed:5" in by_id  # 回填库 2026 年论文入选
    assert "pubmed:6" not in by_id  # 非 2026 年剔除


def test_collection_html_uses_collection_wording(tmp_path):
    """合集 HTML：标题/问候为合集口径，不出现"今日"字样，只收录传入的 Must Read 行。"""
    conn = _seed_db(tmp_path)
    conns = [conn]
    scored = [
        {"paper_id": "pubmed:1", "grade": "Must Read", "total_score": 8.4},
        {"paper_id": "pubmed:2", "grade": "Must Read", "total_score": 7.1},
    ]
    rows = mc.fetch_render_rows(conns, scored)
    conn_of = {r["paper_id"]: conn for r in rows}
    body = mc.build_collection_html(conns, conn_of, rows, "2026", pool_size=2,
                                    user_name="yan-zys", trend=TREND)
    conn.close()
    assert "2026 年度 Must Read 文献合集" in body
    assert "yan-zys，你好" in body
    assert "从全库 2 篇候选中评选出 2 篇论文" in body
    assert "今日" not in body
    assert "Part 1 · Must Read 论文新闻摘要" in body
    assert "Part 3 · 年度 Must Read 文献价值总结" in body
    assert "测试趋势总览" in body
    assert "Single-cell atlas of octopus brain" in body
    assert "章鱼脑单细胞图谱" in body
    assert ">8.4<" in body and ">7.1<" in body
    # 反馈链接与日报一致
    assert "http://127.0.0.1:8710/feedback?paper_id=pubmed%3A1&rating=good" in body
    # 未入选论文不出现
    assert "Soil texture classification" not in body
