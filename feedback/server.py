"""一键反馈本地小服务（http.server，监听 127.0.0.1:8710）。

日报网页版（GitHub Pages）中的按钮通过 JS fetch 调用本服务：

    GET /feedback?paper_id=<id>&rating=good|ok|bad|read|star[&reason=不相关原因]
        点击即把 {"paper_id": .., "rating": ..} 追加写入
        input/user_feedback/YYYY-MM-DD.jsonl（同日同 paper_id+rating 去重；
        带 reason 重复提交时更新原记录的 reason 字段）。
    GET /keywords?text=CRISPR 递送, 单细胞测序
        用户手填的新方向关键词，追加到 input/keyword_requests/YYYY-MM-DD.jsonl
        （{"type": "keywords", "text": ..}），次日流水线入库生效。
    GET /keyword_papers?ids=10.1038/s41586-025-00000-x,40123456
        用户粘贴的参考论文（DOI 或 PMID，逗号/换行分隔，一次 ≤10 篇），
        追加 {"type": "papers", "ids": [..]} 到同一收件箱，次日由
        keyword_engine/apply_page_requests.py 提炼检索词入库。

成功时返回 **204 No Content**，页面原地标记"已记录"、不跳转；参数错误才返回
HTML 说明页。由于页面源是公网 https（GitHub Pages），浏览器对本机地址会发
CORS / Private Network Access 预检，故所有响应带 Access-Control-Allow-Origin: *
与 Access-Control-Allow-Private-Network: true，并实现 do_OPTIONS 应答预检。
仅绑定回环地址，不对外暴露；log_message 静默（访问日志不打到终端）。
"""
import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_DIR = ROOT / "input" / "user_feedback"
REQUESTS_DIR = ROOT / "input" / "keyword_requests"
HOST, PORT = "127.0.0.1", 8710
RATINGS = ("good", "ok", "bad", "read", "star")
MAX_PAPER_IDS = 10

PAGE_ERR = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>参数错误</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2 style="color:#c0392b">参数错误</h2>
<p>{msg}</p>
<p style="color:#888;font-size:13px">正确格式：/feedback?paper_id=...&amp;rating=good|ok|bad|read|star[&amp;reason=...]
　/keywords?text=...　/keyword_papers?ids=DOI或PMID（≤10 个）</p>
</body></html>"""


def append_feedback(paper_id: str, rating: str, reason: str = None, day: str = None) -> Path:
    """追加一条反馈到当日 jsonl；同日同 paper_id+rating 已存在则不重复写
    （带 reason 时改为更新原记录的 reason）。返回文件路径。"""
    day = day or date.today().isoformat()
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = FEEDBACK_DIR / f"{day}.jsonl"
    lines, found_idx = [], None
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue
            if old.get("paper_id") == paper_id and old.get("rating") == rating:
                found_idx = i
                break
    if found_idx is not None:
        if reason:
            old = json.loads(lines[found_idx])
            if old.get("reason") != reason:
                old["reason"] = reason
                lines[found_idx] = json.dumps(old, ensure_ascii=False)
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path  # 已记录过，幂等
    entry = {"paper_id": paper_id, "rating": rating}
    if reason:
        entry["reason"] = reason
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def append_request(entry: dict, day: str = None) -> Path:
    """追加一条关键词/参考论文请求到当日收件箱 jsonl。返回文件路径。"""
    day = day or date.today().isoformat()
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REQUESTS_DIR / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def split_paper_ids(raw: str) -> list:
    """把用户粘贴的 DOI/PMID 文本（逗号/空白/换行分隔）拆成列表，限 MAX_PAPER_IDS。"""
    ids = [s.strip() for s in re.split(r"[,，;；\s]+", raw or "") if s.strip()]
    return ids[:MAX_PAPER_IDS]


class FeedbackHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):  # noqa: N802
        """应答 CORS / Private Network Access 预检（GitHub Pages → 127.0.0.1）。"""
        self.send_response(204)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/feedback":
            self._handle_feedback(qs)
        elif parsed.path == "/keywords":
            self._handle_keywords(qs)
        elif parsed.path == "/keyword_papers":
            self._handle_keyword_papers(qs)
        else:
            self._reply(404, PAGE_ERR.format(msg="未知路径"), "text/html")

    def _handle_feedback(self, qs) -> None:
        paper_id = (qs.get("paper_id") or [""])[0].strip()
        rating = (qs.get("rating") or [""])[0].strip()
        reason = (qs.get("reason") or [""])[0].strip()[:200] or None
        if not paper_id or rating not in RATINGS:
            self._reply(400, PAGE_ERR.format(
                msg=f"paper_id 缺失或 rating 非法（paper_id={paper_id!r}, rating={rating!r}）"),
                "text/html")
            return
        append_feedback(paper_id, rating, reason=reason)
        self._no_content()

    def _handle_keywords(self, qs) -> None:
        text = (qs.get("text") or [""])[0].strip()[:1000]
        if not text:
            self._reply(400, PAGE_ERR.format(msg="text 缺失（关键词内容为空）"), "text/html")
            return
        append_request({"type": "keywords", "text": text})
        self._no_content()

    def _handle_keyword_papers(self, qs) -> None:
        raw = (qs.get("ids") or [""])[0].strip()
        ids = split_paper_ids(raw)
        if not ids:
            self._reply(400, PAGE_ERR.format(msg="ids 缺失（DOI/PMID 为空）"), "text/html")
            return
        append_request({"type": "papers", "ids": ids})
        self._no_content()

    def _no_content(self) -> None:
        """204 No Content：页面 fetch 原地处理，不跳转。"""
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002
        """静默访问日志。"""

    def _cors_headers(self) -> None:
        """允许网页版（公网 https 源）fetch 本机服务。"""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _reply(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer((HOST, PORT), FeedbackHandler)
    print(f"反馈服务已启动：http://{HOST}:{PORT}"
          "/feedback?paper_id=...&rating=good|ok|bad|read|star[&reason=...]；"
          "/keywords?text=...；/keyword_papers?ids=...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
