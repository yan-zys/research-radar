"""SQLite 共用辅助模块（库文件 database/papers.db）。

提供 get_conn() / init_db()，维护三张表：
- papers：抓取的原始文献元数据
- paper_scores：规则过滤分与 AI 评分结果
- recommendations：每日 Top 推荐清单
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "database" / "papers.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    title TEXT,
    abstract TEXT,
    authors TEXT,
    journal TEXT,
    date TEXT,
    doi TEXT,
    url TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS paper_scores (
    paper_id TEXT PRIMARY KEY,
    rule_score REAL,
    passed_filter INTEGER,
    ai_score REAL,
    category TEXT,
    reason TEXT,
    one_line_summary_cn TEXT,
    reproducibility TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT,
    paper_id TEXT,
    total_score REAL,
    grade TEXT
);
"""


def get_conn(db_path=None) -> sqlite3.Connection:
    """返回数据库连接（row_factory=sqlite3.Row），必要时自动建库建表。"""
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """按 SCHEMA 建表（幂等）；对老库补建新列（轻量迁移）。"""
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_scores)")}
    if "abstract_cn" not in cols:
        conn.execute("ALTER TABLE paper_scores ADD COLUMN abstract_cn TEXT")
    conn.commit()
