"""Phase 4：人工审核候选关键词（命令行交互）。

逐条打印候选词 + AI 理由，人工输入 y(approved) / n(rejected) / s(skip)，
结果回写同一个 YAML 文件的 status 字段，该文件本身即审核记录。

注意：本脚本需要交互式终端，请在终端中直接运行：
    venv/bin/python keyword_engine/review_candidates.py \
        --input keyword_engine/output_candidates.yaml
"""
import argparse
from pathlib import Path

import yaml

DECISIONS = {"y": "approved", "n": "rejected", "s": "pending"}


def apply_decision(candidate: dict, choice: str) -> dict:
    candidate["status"] = DECISIONS[choice]
    return candidate


def main():
    ap = argparse.ArgumentParser(description="人工审核候选关键词")
    ap.add_argument("--input", required=True, help="候选词 YAML（审核结果回写同一文件）")
    ap.add_argument("--approve-all", action="store_true",
                    help="非交互模式：把所有 pending 候选一次性置为 approved（用于批量确认后自动执行）")
    args = ap.parse_args()

    path = Path(args.input)
    candidates = yaml.safe_load(path.read_text(encoding="utf-8")) or []

    if args.approve_all:
        for c in candidates:
            if c.get("status") == "pending":
                c["status"] = "approved"
        path.write_text(yaml.dump(candidates, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"已批量 approved {sum(1 for c in candidates if c.get('status') == 'approved')} 条，回写 {path}")
        return

    pending = [i for i, c in enumerate(candidates) if c.get("status") == "pending"]
    if not pending:
        print("没有待审核的候选词。")
        return

    total = len(pending)
    for n, idx in enumerate(pending, 1):
        c = candidates[idx]
        print(f"\n[{n}/{total}] {c['keyword']} (Level {c['level']})")
        print(f"理由: {c.get('reason') or '（无）'}")
        while True:
            choice = input("你的决定 [y/n/s]: ").strip().lower()
            if choice in DECISIONS:
                break
        apply_decision(c, choice)

    path.write_text(yaml.dump(candidates, allow_unicode=True, sort_keys=False), encoding="utf-8")
    counts = {s: sum(1 for c in candidates if c.get("status") == s) for s in DECISIONS.values()}
    print(f"\n审核完成：approved {counts['approved']} / rejected {counts['rejected']} "
          f"/ 待审 {counts['pending']}，已回写 {path}")


if __name__ == "__main__":
    main()
