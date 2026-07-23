"""爬虫共用辅助：关键词加载、--since 日期解析、入库去重。"""
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database.db import get_conn, init_db  # noqa: E402

PAPER_COLS = ("paper_id", "title", "abstract", "authors", "journal", "date", "doi", "url", "source")


def load_keywords() -> list:
    """读取 keyword_config.yaml 所有组的词条，用于构造检索式/本地粗筛。"""
    cfg = yaml.safe_load((ROOT / "config" / "keyword_config.yaml").read_text(encoding="utf-8"))
    kws = []
    for group in (cfg.get("keywords") or {}).values():
        for item in (group or {}).get("items") or []:
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
