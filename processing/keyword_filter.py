"""Phase 8：关键词规则过滤与计分。

对 papers 表中尚无 rule_score 的论文，在 title+abstract 上做大小写不敏感匹配。
ASCII 关键词用词边界匹配并允许复数后缀（防止 "ant" 误中 "important/plant"，
同时 "ants" 仍可命中 "ant"）；非 ASCII（中文等）关键词退化为普通子串匹配：
- negative 命中 → passed_filter=0；
- 否则 rule_score = Σ(命中的不同词条：组 weight × 词条 weight，词条 weight 缺省按 1)，
  passed_filter = 1 if rule_score > 0 else 0。
结果写入 paper_scores（UPSERT）。match_keywords 为纯函数，供 ranking 复用。
"""
import argparse
import re
import sys
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402


def load_config(path=None) -> dict:
    """加载 keyword_config.yaml。"""
    p = Path(path) if path else ROOT / "config" / "keyword_config.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# 特定词条的排除性后缀（负向先行断言）：避免脑/神经单词词条命中机器
# 学习术语——"neural" 不应匹配 "neural network"（工程/算法类论文噪声）
_KW_EXCLUDE_SUFFIX = {"neural": r"(?!\s+networks?\b)"}


@lru_cache(maxsize=None)
def _kw_pattern(kw: str) -> "re.Pattern":
    """编译关键词匹配模式（大小写不敏感）。

    ASCII 且首尾为单词字符的关键词加 \\b 词边界，词尾允许 s/es 复数；
    其余（中文、特殊符号起止）用普通子串匹配。
    _KW_EXCLUDE_SUFFIX 中的词条附带排除性后缀断言。
    """
    pat = re.escape(kw)
    if kw.isascii():
        if re.match(r"\w", kw):
            pat = r"\b" + pat
        pat += _KW_EXCLUDE_SUFFIX.get(kw.lower(), "")
        if re.search(r"\w$", kw):
            pat = pat + r"(?:e?s)?\b"
    return re.compile(pat, re.IGNORECASE)


def match_keywords(title: str, abstract: str, config: dict) -> dict:
    """返回命中明细：{"negative": [...], "hits": [{group, keyword, group_weight, item_weight, score}]}。"""
    text = f"{title or ''} {abstract or ''}"
    negative = [str(kw) for kw in (config.get("negative") or [])
                if _kw_pattern(str(kw)).search(text)]
    hits = []
    for group, gdata in (config.get("keywords") or {}).items():
        gw = (gdata or {}).get("weight", 1)
        for item in (gdata or {}).get("items") or []:
            kw = str(item["keyword"])
            if _kw_pattern(kw).search(text):
                iw = item.get("weight", 1)
                hits.append({"group": group, "keyword": kw,
                             "group_weight": gw, "item_weight": iw, "score": gw * iw})
    return {"negative": negative, "hits": hits}


def rule_score(match: dict) -> float:
    """negative 命中记 0；否则为各命中的不同词条得分之和。"""
    if match["negative"]:
        return 0.0
    return float(sum(h["score"] for h in match["hits"]))


def group_scores(match: dict) -> dict:
    """按组聚合命中得分（ranking 的 method_transfer_value / trend_value 复用）。"""
    agg = {}
    for h in match["hits"]:
        agg[h["group"]] = agg.get(h["group"], 0) + h["score"]
    return agg


def run(conn, config: dict) -> dict:
    """处理所有尚无 rule_score 的论文，UPSERT 写入 paper_scores，返回统计。"""
    rows = conn.execute(
        "SELECT p.paper_id, p.title, p.abstract FROM papers p "
        "LEFT JOIN paper_scores s ON p.paper_id = s.paper_id "
        "WHERE s.rule_score IS NULL"
    ).fetchall()
    passed = 0
    for r in rows:
        m = match_keywords(r["title"], r["abstract"], config)
        score = rule_score(m)
        ok = 0 if m["negative"] else (1 if score > 0 else 0)
        passed += ok
        conn.execute(
            "INSERT INTO paper_scores (paper_id, rule_score, passed_filter) VALUES (?, ?, ?) "
            "ON CONFLICT(paper_id) DO UPDATE SET rule_score=excluded.rule_score, "
            "passed_filter=excluded.passed_filter",
            (r["paper_id"], score, ok),
        )
    conn.commit()
    return {"total": len(rows), "passed": passed, "rejected": len(rows) - passed}


def main():
    ap = argparse.ArgumentParser(description="关键词规则过滤与计分")
    ap.add_argument("--config", default=None, help="keyword_config.yaml 路径")
    ap.add_argument("--db", default=None, help="数据库路径（默认 database/papers.db）")
    args = ap.parse_args()

    conn = get_conn(args.db)
    init_db(conn)
    stats = run(conn, load_config(args.config))
    conn.close()
    print(f"过滤完成：总数 {stats['total']} / 通过 {stats['passed']} / 剔除 {stats['rejected']}")


if __name__ == "__main__":
    main()
