"""Phase 3：AI 关键词第一轮扩充。

读取科研画像（含种子关键词）→ 拼装 Prompt → 调用 AI → 去重截断 →
写出待审核候选词（status: pending），交由 review_candidates.py 人工确认。
"""
import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402

# 按 A > B > C 优先级保留
LEVELS = [
    ("level_A_core_monitor", "A"),
    ("level_B_method_transfer", "B"),
    ("level_C_trend_tracking", "C"),
]


def normalize(kw: str) -> str:
    """字符串归一化：小写、去除非字母数字字符（连字符/空格等）。"""
    return "".join(ch for ch in str(kw).lower() if ch.isalnum())


def load_existing_keywords(path: Path) -> set:
    """加载 keyword_config.yaml 中已有词条（若存在），用于去重。"""
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    existing = set()
    for group in (data.get("keywords") or {}).values():
        for item in (group or {}).get("items") or []:
            existing.add(normalize(item["keyword"]))
    return existing


def collect_input_keywords(profile: dict) -> set:
    """收集画像中所有已有关键词，避免 AI 重复推荐。"""
    kws = set()
    for key in ("research_interest", "keywords", "methods", "species", "tools", "exclude"):
        for item in profile.get(key) or []:
            kws.add(normalize(item))
    return kws


def main():
    ap = argparse.ArgumentParser(description="AI 关键词第一轮扩充")
    ap.add_argument("--input", required=True, help="科研画像 YAML（含种子关键词）")
    ap.add_argument("--output", required=True, help="候选词输出 YAML")
    ap.add_argument("--max-new", type=int, default=20, help="新增候选上限（默认 20）")
    args = ap.parse_args()

    profile = yaml.safe_load(Path(args.input).read_text(encoding="utf-8"))
    template = (ROOT / "prompts" / "expand_keywords_prompt.txt").read_text(encoding="utf-8")
    prompt = template.replace("{{profile}}", yaml.dump(profile, allow_unicode=True, sort_keys=False))

    result = call_model(prompt, response_format="json")
    reasoning = result.get("reasoning") or {}

    seen = collect_input_keywords(profile) | load_existing_keywords(ROOT / "config" / "keyword_config.yaml")
    candidates = []
    for field, level in LEVELS:
        for kw in result.get(field) or []:
            norm = normalize(kw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            candidates.append({
                "keyword": str(kw),
                "level": level,
                "source": "seed",
                "reason": reasoning.get(kw, ""),
                "status": "pending",
            })
            if len(candidates) >= args.max_new:
                break
        if len(candidates) >= args.max_new:
            break

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.dump(candidates, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"新增候选 {len(candidates)} 条（上限 {args.max_new}），已写入 {out}")


if __name__ == "__main__":
    main()
