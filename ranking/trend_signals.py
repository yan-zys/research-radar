"""外部趋势信号：PubMed 近期发文热度 + 顶刊当期命中。

trend_value 维度改为外部信号（不再复用 concept 组关键词命中——那与关键词
通道同源，等于重复计分，且反映的是"是否踩中画像"而非"科研界趋势"）：
- 发文热度（0-8 分）：论文显著词（命中的配置关键词 ∪ 标题特征词）在近 30 天
  PubMed 的发文量（esearch count，POST，≤3 请求/秒，按日缓存
  logs/pubmed_heat_cache.json）；heat = Σ log1p(count)，按候选池最大值归一化
  到 0-8。网络不可用时逐词回退到本地 papers 表近 30 天文档频次（LIKE 匹配）；
- 顶刊当期命中（0-2 分）：期刊在 config/top_journals.yaml 清单内（即本期顶刊
  目录正在推的文章）+2。
trend_value = 热度归一化 + 顶刊加分，封顶 10。count_fn 依赖注入，纯函数部分
（title_terms / salient_terms / heat_of / trend_values）离线可测。
"""
import json
import math
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "logs" / "pubmed_heat_cache.json"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
REQUEST_INTERVAL = 0.4  # 秒，遵守 NCBI ≤3 请求/秒
WINDOW_DAYS = 30
HEAT_SCALE = 8.0         # 热度归一化满分
TOP_JOURNAL_BONUS = 2.0  # 顶刊当期命中加分（HEAT_SCALE + BONUS = 10）

# 通用英语 + 高频科研虚词（这些词命中率高但无趋势区分度）
STOPWORDS = frozenset("""
about above after against along among analyses analysis and approach are based
between both but can cell cells cellular could data disease diseases during
effect effects from function functional gene genes genetic genome genomics has
have how human humans identification insights into its large level like
mechanism mechanisms method methods model models molecular more new novel over
patients protein proteins regulation results reveals role show shown study
system systems than that the their these they this through under using via was
were when which while with within without
""".split())

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{4,}")


def title_terms(title: str, max_terms: int = 5) -> list:
    """标题特征词：≥5 字符的字母/数字/连字符 token，小写去停用词，保序去重取前 max_terms。"""
    terms = []
    for tok in _TOKEN.findall(title or ""):
        t = tok.lower().strip("-")
        if len(t) >= 5 and t not in STOPWORDS and t not in terms:
            terms.append(t)
        if len(terms) >= max_terms:
            break
    return terms


def salient_terms(match: dict, title: str, max_title_terms: int = 5) -> list:
    """显著词 = 命中的配置关键词 ∪ 标题特征词（标题词已被某关键词覆盖则跳过，防重复计数）。"""
    terms, seen = [], set()
    for h in match.get("hits", []):
        kw = str(h["keyword"])
        if kw.lower() not in seen:
            seen.add(kw.lower())
            terms.append(kw)
    for t in title_terms(title, max_title_terms):
        if not any(t in kw or kw in t for kw in seen):
            seen.add(t)
            terms.append(t)
    return terms


def heat_of(terms, count_fn) -> float:
    """Σ log1p(count)；count 为 None（查询失败）或 0 的词不计。"""
    total = 0.0
    for t in terms:
        c = count_fn(t)
        if c:
            total += math.log1p(c)
    return total


def trend_values(heats: dict, is_tj: dict) -> dict:
    """热度按池内最大值归一化到 0-8，顶刊当期命中 +2，封顶 10。全零池全为 0。"""
    max_heat = max(heats.values(), default=0)
    if not max_heat:
        return {pid: 0.0 for pid in heats}
    return {pid: min(10.0, h / max_heat * HEAT_SCALE
                     + (TOP_JOURNAL_BONUS if is_tj.get(pid) else 0.0))
            for pid, h in heats.items()}


def esearch_count(term: str, since: date, until: date) -> int:
    """PubMed 近 N 天该词 title/abstract 命中的发文量（retmax=0 只取 count）。"""
    resp = requests.post(f"{EUTILS}/esearch.fcgi", data={
        "db": "pubmed", "term": f'"{term}"[Title/Abstract]', "retmax": 0,
        "retmode": "json", "datetype": "pdat",
        "mindate": since.strftime("%Y/%m/%d"), "maxdate": until.strftime("%Y/%m/%d"),
    }, timeout=30)
    resp.raise_for_status()
    return int(resp.json().get("esearchresult", {}).get("count", 0))


class PubMedCounter:
    """term → 近 N 天 PubMed 发文量；按日 JSON 缓存，网络异常返回 None（交给回退）。"""

    def __init__(self, end: date, days: int = WINDOW_DAYS, cache_path=CACHE_PATH,
                 interval: float = REQUEST_INTERVAL):
        self.since = end - timedelta(days=days)
        self.end = end
        self.interval = interval
        self.cache_path = Path(cache_path)
        self.dirty = False
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            self.cache = {}

    def __call__(self, term: str):
        key = term.lower()
        ent = self.cache.get(key)
        if ent and ent.get("date") == self.end.isoformat():
            return ent["count"]
        try:
            time.sleep(self.interval)
            n = esearch_count(term, self.since, self.end)
        except Exception:
            return None
        self.cache[key] = {"date": self.end.isoformat(), "count": n}
        self.dirty = True
        return n

    def flush(self):
        if self.dirty:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
            self.dirty = False


def corpus_count_fn(conn, end: date, days: int = WINDOW_DAYS):
    """本地 papers 表近 N 天文档频次（title/abstract LIKE，按词记忆）。"""
    since = (end - timedelta(days=days)).isoformat()
    until = end.isoformat()
    memo = {}

    def count(term: str) -> int:
        key = term.lower()
        if key not in memo:
            like = f"%{term}%"
            memo[key] = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE date BETWEEN ? AND ? "
                "AND (title LIKE ? OR abstract LIKE ?)",
                (since, until, like, like)).fetchone()[0]
        return memo[key]

    return count


def combined_count_fn(primary, fallback):
    """primary 返回 None（网络失败）的词用 fallback 计数；flush 透传 primary。"""
    def count(term):
        c = primary(term)
        return c if c is not None else fallback(term)
    count.flush = getattr(primary, "flush", lambda: None)
    return count


def build_trend_ctx(conn, end_date: str, offline: bool = False) -> dict:
    """构造 compute_scores 的趋势上下文 {count_fn, tj_names}。

    offline=False：PubMed 发文热度为主、本地语料库频次为逐词回退；
    offline=True：只用本地语料库（测试/无网环境）。end_date 为热度窗口右端
    （YYYY-MM-DD，窗口 = [end-30d, end]）。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from crawler.top_journals import journal_names

    end = date.fromisoformat(end_date)
    corpus = corpus_count_fn(conn, end)
    count_fn = corpus if offline else combined_count_fn(PubMedCounter(end), corpus)
    return {"count_fn": count_fn, "tj_names": set(journal_names())}
