"""爬虫共用辅助：关键词加载、--since 日期解析、入库去重。"""
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

PAPER_COLS = ("paper_id", "title", "abstract", "authors", "journal", "date", "doi", "url", "source")


def load_keywords(path=None) -> list:
    """读取 keyword_config.yaml 中参与召回的词条，用于构造检索式/本地粗筛。

    组级或词条级标注 recall: false 的关键词不参与召回——过泛的词
    （brain/neuron/neural/glia/Cell atlas 及 species 整组，2026-08-05 用户
    指定）只靠它们命中会把大量噪声论文拉进库；它们仍在 keyword_filter /
    scoring 中正常计分，为已被其他词召回的论文加分。
    """
    p = Path(path) if path else ROOT / "config" / "keyword_config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    kws = []
    for group in (cfg.get("keywords") or {}).values():
        if (group or {}).get("recall") is False:
            continue
        for item in (group or {}).get("items") or []:
            if item.get("recall") is False:
                continue
            kws.append(str(item["keyword"]))
    return kws


def resolve_since(s: str) -> date:
    """--since 参数解析：yesterday 解析为昨天，否则按 YYYY-MM-DD。"""
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(s)


def save_papers(papers, db_path=None) -> int:
    """INSERT OR IGNORE 入库（按 paper_id 去重），返回实际插入条数。"""
    conn = get_conn(db_path)
    init_db(conn)
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO papers ({', '.join(PAPER_COLS)}) "
        f"VALUES ({', '.join('?' * len(PAPER_COLS))})",
        [tuple(p.get(c) for c in PAPER_COLS) for p in papers],
    )
    conn.commit()
    n = conn.total_changes - before
    conn.close()
    return n
