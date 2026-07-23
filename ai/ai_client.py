"""统一 AI API 调用封装（OpenAI 兼容接口）。

从 .env 读取 KIMI_API_KEY，从 config/model.yaml 读取 base_url / model / 温度 / 超时。
"""
import json
import time
from pathlib import Path

import requests
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def _load_config():
    cfg = yaml.safe_load((ROOT / "config" / "model.yaml").read_text(encoding="utf-8"))
    api_key = (dotenv_values(ROOT / ".env").get("KIMI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("KIMI_API_KEY 未在 .env 中配置")
    return cfg, api_key


def _extract_json(text: str) -> dict:
    """从模型输出中解析 JSON，容忍代码块包裹。"""
    text = text.strip()
    if text.startswith("```"):
        lines = [l for l in text.splitlines() if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    return json.loads(text)


def _iter_sse_lines(resp):
    """字节级缓冲的 SSE 行迭代器，保证多字节 UTF-8 字符不被 chunk 边界切断。"""
    buf = b""
    for chunk in resp.iter_content(chunk_size=None):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8").rstrip("\r")
    if buf:
        yield buf.decode("utf-8")


def _stream_chat(url: str, headers: dict, payload: dict, timeout: int) -> str:
    """流式请求并拼接 content，避免 thinking 模型长响应被网关超时切断。"""
    parts = []
    with requests.post(url, headers=headers, json=payload, stream=True,
                       timeout=(10, timeout)) as resp:
        resp.raise_for_status()
        for line in _iter_sse_lines(resp):
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
    return "".join(parts)


def call_model(prompt: str, response_format: str = "json"):
    """调用模型。response_format="json" 时返回解析后的 dict，否则返回原始文本。"""
    cfg, api_key = _load_config()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": cfg.get("temperature", 1),
        "stream": True,
    }
    timeout = cfg.get("timeout", 60)

    last_err = None
    for attempt in range(3):
        try:
            content = _stream_chat(url, headers, payload, timeout)
            if response_format != "json":
                return content
            try:
                return _extract_json(content)
            except json.JSONDecodeError:
                # 落盘原始输出便于诊断
                logs = ROOT / "logs"
                logs.mkdir(exist_ok=True)
                (logs / "last_model_output.txt").write_text(content, encoding="utf-8")
                raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"call_model 失败（已重试 2 次）: {last_err}")
