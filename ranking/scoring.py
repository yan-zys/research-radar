"""四维加权打分与每日 Top 推荐。

各维度归一化到 0-10 后按 config/scoring.yaml 权重加权（total_score 0-10）：
- research_relevance：rule_score 除以候选池最大值 ×10
- ai_semantic_relevance：ai_score（缺失按 5；已评且 <3 的论文为 AI 否决，仅作兜底层候选）
- journal_influence：内置期刊分档 dict
- trend_value：外部趋势信号（ranking/trend_signals.py：PubMed 近 30 天发文热度
  归一化 0-8 + 顶刊当期命中 +2；offline 模式用本地语料库文档频次）
每日 Top 15 按五层梯队兜底选满（A0 层为 core 组脑/神经关键词命中、或 core_broad 泛词
与组学锚点共现，必推送；run() 详见其
docstring），全部写入 recommendations，
不再因总分低剔除（grade：≥7 Must Read / ≥5 Important / 其余 Relate——纯总分定级，
不设 AI 分门槛，2026-08-05 用户确认；同日期先清空旧记录再写入）。
打印各层选取数与 Top15 简表。
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402
from processing.keyword_filter import match_keywords  # noqa: E402
from ranking.trend_signals import build_trend_ctx, heat_of, salient_terms, trend_values  # noqa: E402


# 组学锚点词：core_broad 组（brain、neuron、neurodevelopment 等广义脑/神经词）必须与
# 这些词在标题+摘要中共现，才视为 A0 核心命中——大方向限定为单细胞转录组、基因组、
# 空间转录组等组学研究，单独的临床/行为/材料类脑神经论文不再必推送
# （2026-07-31 用户反馈：TRAb 队列、ECHO 金属暴露、ABCD 空气污染、GFAP 抗体、
# 茶水蛋水凝胶微针等纯临床/材料论文被 A0 强推混入日报）。
# 2026-08-04 收紧：裸 "spatial" 移出锚点（"spatial resolution/memory" 等行为/
# 成像论文大量漏入；空间组学论文本就命中 transcriptom/genom/"spatial omics"）。
OMICS_ANCHORS = (
    "single-cell", "single cell", "single-nucleus", "single nucleus",
    "transcriptom", "genom", "spatial omics", "chromatin", "atac", "epigenom",
    "cis-regulatory", "regulatory element", "gene regulatory", "cell atlas",
    "multi-omics", "multiomics", "proteom", "pangenom", "metagenom",
)


def section_of(r) -> str:
    """从行（sqlite3.Row 或 dict）取 AI 分区字段 section，缺列/缺键时返回 None。"""
    try:
        return r["section"]
    except (KeyError, IndexError):
        return None


def row_text(r) -> str:
    """安全拼接（标题+摘要）小写文本；缺 abstract 列/键时只用标题。"""
    try:
        ab = r["abstract"]
    except (KeyError, IndexError):
        ab = ""
    return ((r["title"] or "") + " " + (ab or "")).lower()


# 图谱标题启发式的锚点与排除信号（is_atlas_candidate 用）
ATLAS_SC_ANCHORS = (
    "single-cell", "single cell", "single-nucleus", "single nucleus",
    "scrna", "snrna", "spatial transcriptom", "spatial omics", "spatial genomics",
    "cell atlas", "multiome", "multi-ome",
)
ATLAS_DOMAIN = (
    # 神经系统（脑图谱最优先）
    "nervous", "brain", "neural", "neuron", "glia", "cortex", "cerebell",
    "spinal cord", "retina", "gangli",
    # 演化/跨物种框架
    "evolution", "origin", "ancestral", "macroevolution", "cross-species",
    "multi-species", "comparative",
    # 非模式/广义动物（独立成词的空格边界防 important→"ant " 类误命中）
    "octopus", "squid", "cephalopod", "cuttlefish", "nautilus",
    "zebrafish", "medaka", "stickleback", "salmon", "teleost", "lamprey",
    "hagfish", "ciona", "amphioxus", "tunicate", "echinoderm", "sea urchin",
    "starfish", "hemichordate", "annelid", "platynereis", "capitella",
    "polychaete", "leech", "arthropod", "insect", "drosophila", " ant ",
    " bee ", "wasp", "beetle", "butterfly", "moth", "crustacean", "shrimp",
    "crab", "spider", "scorpion", "centipede", "millipede", "onychophora",
    "velvet worm", "mollusc", "mollusk", "snail", "oyster", "clam", "bivalve",
    "gastropod", "planarian", "flatworm", "nematode", "c. elegans", "worm",
    "hydra", "cnidaria", "jellyfish", "coral", "sponge", "ctenophor",
    "placozo", "rotifer", "tardigrade", "xenopus", "frog", "salamander",
    "axolotl", "lizard", "gecko", "turtle", "bird", "chicken", "finch",
    " bat ", "platypus", "marsupial", "opossum", "primate", "macaque",
    "marmoset", "tree shrew",
    # 全生物体图谱
    "whole-organism", "whole organism", "whole-animal", "whole animal",
    "organism-wide", "whole-body", "whole body",
)
ATLAS_TITLE_BLOCK = (
    # 非动物（植物/微生物）
    "arabidopsis", "maize", "rice", "wheat", "soybean", "tomato", "plant",
    "alga", "fung", "yeast", "bacteri", "microbiom", "microbiota", "viru",
    "phage", "archaea", "plasmodium",
    # 计算方法/工具（非图谱发布）
    "foundation model", "benchmark", "algorithm", "framework", "toolkit",
    "web server", "database", "in-memory", "cloud", "gpu", "software",
    "package", "wasserstein", "optimal transport", "graph neural",
    "deep learning", "machine learning", "imputation", "deconvolution",
    # 人类疾病/临床
    "glaucom", "cancer", "tumor", "tumour", "carcinoma", "leukemia",
    "lymphoma", "diabetes", "alzheimer", "parkinson", "disease", "patient",
    "clinical", "covid", "fibrosis", "arthritis", "therap", "injury",
)


def is_atlas_candidate(title: str, text_lower: str) -> bool:
    """新动物单细胞图谱的标题启发式兜底（AI 分区 section=="atlas" 之外的保险）：
    标题含 atlas + 文本命中单细胞组学锚点 + 文本命中领域信号（神经系统/演化/
    非模式动物/全生物体）且标题无排除信号（植物/微生物/计算方法/人类疾病）。

    图谱论文若未过 paper_analyzer（section 为 NULL），不能仅凭关键词分被埋成
    Relate——2026-10-14 线虫 L1 神经图谱以 4.59 Relate 入选的教训。领域信号
    只匹配标题（摘要里 evolution/origin/comparative 太常见，会误捞人类关节
    图谱、植物种子图谱等——同日二版启发式的教训）；未命中领域信号的模式
    动物组织图谱（如 mouse ovary）走正常评分。
    """
    t = (title or "").lower()
    return ("atlas" in t
            and any(a in text_lower for a in ATLAS_SC_ANCHORS)
            and any(a in t for a in ATLAS_DOMAIN)
            and not any(a in t for a in ATLAS_TITLE_BLOCK))


def norm_title(t: str) -> str:
    """规范化标题：小写、非字母数字折叠为单空格——用于跨源去重
    （bioRxiv 预印本与正式发表版 paper_id 不同但标题相同，视为同一篇）。"""
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

# 演化锚点：core_broad 组泛词（brain、neuron 等）进 A0 除组学锚点外，还须与
# 下列演化/比较视角词共现——用户方向是"脑/神经的演化"，纯神经机制论文
# （癫痫、示踪、工程神经组织等）即使做单细胞/组学也不再必推送
# （2026-08-04 用户确认，对照其 5 篇方向范例：lamprey/octopus 脑图谱、
# WGD 与细胞类型演化、跨物种深度学习等均以演化/比较为框架）。
EVOLUTION_ANCHORS = (
    "evolution", "origin", "ancestral", "homolog", "conserved", "conservation",
    "cross-species", "comparative", "phylogen", "diversification",
    "evo-devo", "evolutionary developmental",
    "whole-genome duplication", "genome duplication", "ohnolog",
)


def journal_score(journal: str) -> float:
    """内置期刊分档：Nature/Science/Cell 及子刊=10，eLife/PLOS=7，预印本=5，默认=5。

    预印本（bioRxiv/arXiv）2026-08-05 由 7 降回 5（用户确认"4-5"区间，取与普通
    期刊同级）：7 分保底贡献 2.1（权重 0.3）曾导致 AI 判定无关（1-2 分）的预印本
    total 仍 ≥5 被标 Important；但降到 4 会让优质预印本排在无名期刊（默认 5）之后。
    取 5 后纯总分定级下 AI 1-2 分的预印本总分约 4.5，自然落在 5 分线下归 Relate，
    期刊分只影响同级内排序，不再能抬噪声上 Important。
    """
    j = (journal or "").strip().lower()
    if "biorxiv" in j or "arxiv" in j:
        return 5.0
    if j.startswith(("nature", "science", "cell")) or j.startswith(("nat ", "nat.")):
        return 10.0
    if "elife" in j or "plos" in j:
        return 7.0
    return 5.0


def grade_of(total: float) -> str:
    """等级分档：≥7 Must Read / ≥5 Important / 其余 Relate（全部入选，不剔除）。

    2026-08-05 用户确认：纯总分定级，不设 AI 分门槛——加权总分已融合
    关键词/AI/期刊/趋势四维，等级直接反映总分高低。
    """
    if total >= 7:
        return "Must Read"
    if total >= 5:
        return "Important"
    return "Relate"


def load_weights(path=None) -> dict:
    """加载 config/scoring.yaml 的 weights。"""
    p = Path(path) if path else ROOT / "config" / "scoring.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("weights") or {}


def load_kw_config() -> dict:
    """加载 keyword_config.yaml（显著词提取复用其命中逻辑）。"""
    return yaml.safe_load((ROOT / "config" / "keyword_config.yaml").read_text(encoding="utf-8"))


def compute_scores(rows, weights: dict, kw_config: dict, ai_veto: float = 3.0,
                   include_vetoed: bool = False, trend_ctx: dict = None) -> list:
    """对候选论文计算四维得分与 total_score（rows 为 papers JOIN paper_scores）。

    AI 否决：ai_score 已评且低于 ai_veto（默认 3）的论文默认直接剔除，
    避免规则分高但 AI 判定无关的论文混入推荐；include_vetoed=True 时保留
    （供 run() 的 C/D 兜底层统一算分，由调用方自行分层排序）。
    trend_ctx（build_trend_ctx 返回的 {count_fn, tj_names}）提供外部趋势信号；
    缺省时 trend_value 全部按 0 计（离线纯计算场景）。
    """
    enriched = []
    for r in rows:
        if not include_vetoed and r["ai_score"] is not None and float(r["ai_score"]) < ai_veto:
            continue
        e = {
            "paper_id": r["paper_id"], "title": r["title"], "journal": r["journal"],
            "rule_score": r["rule_score"] or 0,
            "ai_score": r["ai_score"] if r["ai_score"] is not None else 5,
            "section": section_of(r),
            "atlas_candidate": is_atlas_candidate(r["title"], row_text(r)),
        }
        if trend_ctx is not None:
            terms = salient_terms(match_keywords(r["title"], r["abstract"], kw_config),
                                  r["title"])
            e["trend_heat"] = heat_of(terms, trend_ctx["count_fn"])
            e["is_tj"] = (r["journal"] or "").strip().lower() in trend_ctx["tj_names"]
        enriched.append(e)
    if trend_ctx is not None:
        tv = trend_values(
            {e["paper_id"]: e["trend_heat"] for e in enriched},
            {e["paper_id"]: e.get("is_tj", False) for e in enriched})
    else:
        tv = {}
    max_rule = max((e["rule_score"] for e in enriched), default=0) or 1
    for e in enriched:
        dims = {
            "research_relevance": e["rule_score"] / max_rule * 10,
            "ai_semantic_relevance": float(e["ai_score"]),
            "journal_influence": journal_score(e["journal"]),
            "trend_value": tv.get(e["paper_id"], 0.0),
        }
        e["total_score"] = round(sum(dims[k] * weights.get(k, 0) for k in dims), 2)
        e["grade"] = grade_of(e["total_score"])
        # 新动物单细胞图谱（AI 分区 section=="atlas"，或标题启发式命中）强制必读——
        # 2026-10-13 用户要求：现代动物单细胞图谱发布（尤其脑图谱）权重最高
        if e["section"] == "atlas" or e["atlas_candidate"]:
            e["grade"] = "Must Read"
    return enriched


def run(conn, weights: dict, kw_config: dict, run_date=None, top_n: int = 15,
        ai_veto: float = 3.0, pub_date=None, offline: bool = False) -> list:
    """对候选论文打分，按五层梯队兜底选满 Top N 写入 recommendations（同日期先清空）。

    梯队（逐层补足 top_n 为止，每层内部按 total_score 降序，分层规则见 layered()）：
    - A0 层：命中 core 组关键词（脑/神经核心方向，命中必推送，绕过 AI 否决），
      或 core_broad 组泛词（brain/neuron/neurodevelopment 等）与组学锚点
      + 演化锚点（EVOLUTION_ANCHORS）双共现，或新动物单细胞图谱
      （AI 分区 section=="atlas"，未过 AI 的由标题启发式 is_atlas_candidate
      兜底识别）——图谱强制 Must Read；
    - A 层：关键词通道、未被 AI 否决、不曾进过 recommendations（全历史去重）；
    - B 层：顶刊通道、已评 AI、未被否决、全历史去重；
    - C 层：被 AI 否决的关键词/顶刊论文、全历史去重；
    - D 层：90 天去重窗口（按 A0→C 同序，排除已入选与近 90 天已推荐）。
    总分仍由 compute_scores 统一计算（同一归一化池，含被否决论文）；每条结果带
    "tier" 字段（"A0"|"A"|"B"|"C"|"D"）标记来源层。仅当库内论文总数不足 top_n 时
    才返回少于 top_n 篇。全历史去重（2026-10-13 用户要求，心脏图谱曾因 30 天窗口
    过期被重复推送 3 次）：凡进过 recommendations 的论文不再参与 A0/A/B/C 层
    候选；另按规范化标题跨源去重（bioRxiv 预印本与正式发表版标题相同但
    paper_id 不同，视为同一篇）。D 兜底层放宽为 90 天窗口，允许更早的旧论文回补。
    pub_date（YYYY-MM-DD）：限定候选论文的发表日期（papers.date），用于补算
    某一天发表文献的历史日报；默认 None 不过滤（日常流程靠爬虫 --since 限制）。
    offline=True 时趋势维度只用本地语料库频次（不访问 PubMed，测试/无网用）。
    """
    if run_date is None:
        d = date.today().isoformat()
    elif isinstance(run_date, str):
        d = run_date
    else:
        d = run_date.isoformat()

    trend_ctx = build_trend_ctx(conn, d, offline=offline)
    tj_names = trend_ctx["tj_names"]

    def fetch(where=None, params=()):
        sql = ("SELECT p.paper_id, p.title, p.abstract, p.journal, "
               "s.rule_score, s.ai_score, s.passed_filter, s.section "
               "FROM papers p JOIN paper_scores s ON p.paper_id = s.paper_id")
        if pub_date is not None:
            sql += " WHERE p.date = ?"
            params = (pub_date,) + tuple(params)
            if where:
                sql += " AND " + where
        elif where:
            sql += " WHERE " + where
        return conn.execute(sql, params).fetchall()

    def layered(rows, hist_titles=None) -> list:
        """统一算分后按 A0→A→B→C 分层排序（层内 total_score 降序），每条带 tier 标记。

        分层规则（顶刊通道上线后收紧，非顶刊且未过关键词过滤的论文一律不推荐，
        杜绝医学噪声兜底混入）：
        - A0 层：命中 core 组（脑/神经核心方向）关键词的论文——用户要求"命中必
          推送"，故置于最前且绕过 AI 否决（negative 排除词仍优先剔除）；
          AI 分区为新动物单细胞图谱（section=="atlas"）的论文同进 A0 且强制
          Must Read（2026-10-13 用户要求：图谱发布权重最高）；未过 AI 的论文
          由标题启发式（标题含 atlas + 组学锚点）兜底识别，同样进 A0；
          core_broad 组泛词（brain、neuron、neurodevelopment 等）须与 OMICS_ANCHORS
          组学锚点（single-cell/transcriptom/genom 等）和 EVOLUTION_ANCHORS
          演化锚点（evolution/origin/ancestral/homolog/conserved/comparative 等）
          双共现才进 A0，否则按普通关键词通道走 A/C 层——避免纯临床/行为/
          材料论文和纯神经机制论文（无演化/比较视角）被强推；
        - A 层：passed_filter=1、未被 AI 否决（关键词通道核心候选）；
        - B 层：顶刊清单内期刊、已评 AI（ai_score 非空）、未被否决；
        - C 层：被 AI 否决（ai_score<3）但属于关键词通过或顶刊清单的论文；
        - 其余（非顶刊 + 规则未命中）：不推荐。
        negative 排除词命中的论文（医学/临床噪声）在所有层都剔除——既是用户
        的降噪要求，也避免日报正文含大量 cancer/tumor/clinical 词汇被邮箱
        反垃圾系统拦截（SMTP 550）。
        """
        matches = {}
        texts = {}
        kept = []
        for r in rows:
            # 规范化标题跨源去重：标题与历史推荐相同的另一 paper_id（如
            # bioRxiv 预印本→正式发表版）直接跳过
            if hist_titles and norm_title(r["title"]) in hist_titles:
                continue
            m = match_keywords(r["title"], r["abstract"], kw_config)
            if m["negative"]:
                continue
            matches[r["paper_id"]] = m
            texts[r["paper_id"]] = ((r["title"] or "") + " " + (r["abstract"] or "")).lower()
            kept.append(r)
        rows = kept

        def core_hit(pid) -> bool:
            hits = matches[pid]["hits"]
            if any(h["group"] == "core" for h in hits):
                return True
            # core_broad（广义脑/神经词）必须与组学锚点 + 演化锚点双共现
            # 才算核心命中（2026-08-04 用户确认：纯神经机制论文不强推）
            if not any(h["group"] == "core_broad" for h in hits):
                return False
            return (any(a in texts[pid] for a in OMICS_ANCHORS)
                    and any(a in texts[pid] for a in EVOLUTION_ANCHORS))

        crit = {}
        for r in rows:
            is_tj = (r["journal"] or "").strip().lower() in tj_names
            veto = r["ai_score"] is not None and float(r["ai_score"]) < ai_veto
            if (core_hit(r["paper_id"]) or section_of(r) == "atlas"
                    or is_atlas_candidate(r["title"], texts[r["paper_id"]])):
                # atlas（新动物单细胞图谱）与 core 命中同级：必推送、排最前
                tier = "A0"
            elif veto:
                tier = "C" if (r["passed_filter"] == 1 or is_tj) else None
            elif r["passed_filter"] == 1:
                tier = "A"
            elif is_tj and r["ai_score"] is not None:
                tier = "B"
            else:
                tier = None
            if tier:
                crit[r["paper_id"]] = tier
        eligible = [r for r in rows if r["paper_id"] in crit]
        scored = compute_scores(eligible, weights, kw_config, ai_veto=ai_veto,
                                include_vetoed=True, trend_ctx=trend_ctx)
        by_tier = {"A0": [], "A": [], "B": [], "C": []}
        for e in scored:
            e["tier"] = crit[e["paper_id"]]
            by_tier[e["tier"]].append(e)
        out = []
        for t in ("A0", "A", "B", "C"):
            by_tier[t].sort(key=lambda e: e["total_score"], reverse=True)
            out.extend(by_tier[t])
        return out

    def hist_titles(where, params) -> set:
        """历史推荐论文的规范化标题集合（跨源去重用），窗口与对应 SQL 去重一致。"""
        rows = conn.execute(
            "SELECT p.title FROM recommendations r "
            "JOIN papers p ON r.paper_id = p.paper_id WHERE " + where, params).fetchall()
        return {norm_title(r["title"]) for r in rows}

    # A0/A/B/C 层：全历史去重（进过 recommendations 的论文不再入选）
    dedup = "p.paper_id NOT IN (SELECT paper_id FROM recommendations WHERE date < ?)"
    top = layered(fetch(dedup, (d,)), hist_titles("r.date < ?", (d,)))[:top_n]
    if len(top) < top_n:  # D 层：90 天去重窗口，按 A0→C 同序补足
        chosen = {e["paper_id"] for e in top}
        d_dedup = ("p.paper_id NOT IN (SELECT paper_id FROM recommendations "
                   "WHERE date >= date(?, '-90 days') AND date < ?)")
        d_hist = hist_titles("r.date >= date(?, '-90 days') AND r.date < ?", (d, d))
        for e in layered(fetch(d_dedup, (d, d)), d_hist):
            if len(top) >= top_n:
                break
            if e["paper_id"] in chosen:
                continue
            e["tier"] = "D"
            top.append(e)
            chosen.add(e["paper_id"])
    conn.execute("DELETE FROM recommendations WHERE date = ?", (d,))
    conn.executemany(
        "INSERT INTO recommendations (date, paper_id, total_score, grade) VALUES (?, ?, ?, ?)",
        [(d, e["paper_id"], e["total_score"], e["grade"]) for e in top],
    )
    conn.commit()
    getattr(trend_ctx["count_fn"], "flush", lambda: None)()
    counts = {t: sum(1 for e in top if e["tier"] == t) for t in ("A0", "A", "B", "C", "D")}
    print(f"梯队选取：A0={counts['A0']} A={counts['A']} B={counts['B']} C={counts['C']} "
          f"D={counts['D']}，共 {len(top)} 篇")
    return top


def main():
    ap = argparse.ArgumentParser(description="四维加权打分与每日 Top 推荐")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    ap.add_argument("--top", type=int, default=15, help="Top N（默认 15）")
    ap.add_argument("--date", default=None,
                    help="推荐日期 YYYY-MM-DD（默认今天；补算历史日报用）")
    ap.add_argument("--pub-date", default=None,
                    help="限定候选论文发表日期 YYYY-MM-DD（默认不过滤；"
                         "补算某日发表文献的历史日报用）")
    ap.add_argument("--offline", action="store_true",
                    help="趋势维度只用本地语料库频次，不访问 PubMed（无网/调试用）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    top = run(conn, load_weights(), load_kw_config(), run_date=args.date,
              top_n=args.top, pub_date=args.pub_date, offline=args.offline)
    conn.close()
    if not top:
        print("暂无论文可推荐（papers 表为空）")
        return
    print(f"Top {len(top)} 推荐：")
    for i, e in enumerate(top, 1):
        title = (e["title"] or "")[:60]
        print(f"{i:2d}. [{e['total_score']:5.2f}] {e['grade']:<10} "
              f"{e['tier']}层  {e['paper_id']}  {title}")


if __name__ == "__main__":
    main()
