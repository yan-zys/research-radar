"""一键反馈本地小服务（http.server，监听 127.0.0.1:8710）。

日报邮件中的反馈链接指向本服务：
    GET /feedback?paper_id=<id>&rating=good|ok|bad|read|star
点击即把 {"paper_id": .., "rating": ..} 追加写入 input/user_feedback/YYYY-MM-DD.jsonl
（同日同 paper_id+rating 去重），浏览器返回"已记录"中文页面，不再弹邮件客户端。
仅绑定回环地址，不对外暴露；log_message 静默（访问日志不打到终端）。
"""
import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_DIR = ROOT / "input" / "user_feedback"
HOST, PORT = "127.0.0.1", 8710
RATINGS = ("good", "ok", "bad", "read", "star")

PAGE_OK = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>反馈已记录</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2 style="color:#0b5cad">已记录，感谢反馈！</h2>
<p>paper_id: {pid}<br>rating: {rating}</p>
<p style="color:#888;font-size:13px">本页可关闭。反馈将在次日日报 pipeline 中参与关键词学习。</p>
</body></html>"""

PAGE_ERR = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>参数错误</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px">
<h2 style="color:#c0392b">参数错误</h2>
<p>{msg}</p>
<p style="color:#888;font-size:13px">正确格式：/feedback?paper_id=...&amp;rating=good|ok|bad|read|star</p>
</body></html>"""


def append_feedback(paper_id: str, rating: str, day: str = None) -> Path:
    """追加一条反馈到当日 jsonl；同日同 paper_id+rating 已存在则不重复写。返回文件路径。"""
    day = day or date.today().isoformat()
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    path = FEEDBACK_DIR / f"{day}.jsonl"
    entry = {"paper_id": paper_id, "rating": rating}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue
            if old.get("paper_id") == paper_id and old.get("rating") == rating:
                return path  # 已记录过，幂等
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


class FeedbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/feedback":
            self._reply(404, PAGE_ERR.format(msg="未知路径"), "text/html")
            return
        qs = parse_qs(parsed.query)
        paper_id = (qs.get("paper_id") or [""])[0].strip()
        rating = (qs.get("rating") or [""])[0].strip()
        if not paper_id or rating not in RATINGS:
            self._reply(400, PAGE_ERR.format(
                msg=f"paper_id 缺失或 rating 非法（paper_id={paper_id!r}, rating={rating!r}）"),
                "text/html")
            return
        append_feedback(paper_id, rating)
        self._reply(200, PAGE_OK.format(pid=paper_id, rating=rating), "text/html")

    def log_message(self, format, *args):  # noqa: A002
        """静默访问日志。"""

    def _reply(self, code: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer((HOST, PORT), FeedbackHandler)
    print(f"反馈服务已启动：http://{HOST}:{PORT}/feedback?paper_id=...&rating=good|ok|bad|read|star")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
