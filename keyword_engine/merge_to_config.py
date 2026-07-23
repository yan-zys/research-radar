"""Phase 6：把 approved 候选词合并进 config/keyword_config.yaml。

规则：
- 只合并 status: approved 的词条，自动填充 added_date 与 source；
- 首次运行时，先把科研画像中的词条作为 source: seed 写入（种子词权重最高）；
- 候选词分组（species/methods/tools/concept）通过与画像对应类别做归一化匹配判定，
  匹配不到归入 concept；
- status: rejected 的词条归档到 keyword_engine/archive_rejected.yaml，
  避免下次扩充重复推荐；
- --dry-run 只预览不写入。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from keyword_engine.expand_keywords import normalize  # noqa: E402

GROUPS = {"species": 10, "methods": 8, "tools": 8, "concept": 5}
PROFILE_GROUP_KEYS = {"species": "species", "methods": "methods", "tools": "tools"}


def classify(keyword: str, profile: dict) -> str:
    """按画像类别匹配分组，匹配不到归入 concept。"""
    norm = normalize(keyword)
    for group, key in PROFILE_GROUP_KEYS.items():
        for item in profile.get(key) or []:
            n = normalize(item)
            if n and (n in norm or norm in n):
                return group
    return "concept"


def seed_items(profile: dict) -> list:
    """画像词条转为种子项：species/methods/tools 归对应组，其余归 concept。"""
    items = []
    for key in ("species", "methods", "tools"):
        for kw in profile.get(key) or []:
            items.append((str(kw), key))
    for key in ("research_interest", "keywords"):
        for kw in profile.get(key) or []:
            items.append((str(kw), "concept"))
    return items


def main():
    ap = argparse.ArgumentParser(description="合并 approved 候选词到 keyword_config.yaml")
    ap.add_argument("--candidates", default=str(ROOT / "keyword_engine" / "output_candidates.yaml"))
    ap.add_argument("--profile", default=str(ROOT / "input" / "seed_keywords.txt"))
    ap.add_argument("--config", default=str(ROOT / "config" / "keyword_config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写入")
    args = ap.parse_args()

    today = date.today().isoformat()
    profile = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
    candidates = yaml.safe_load(Path(args.candidates).read_text(encoding="utf-8")) or []

    config_path = Path(args.config)
    if config_path.exists():
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    else:
        # 首次运行：用画像词条初始化，source 标记为 seed
        config = {"keywords": {g: {"weight": w, "items": []} for g, w in GROUPS.items()},
                  "negative": profile.get("exclude") or []}
        for kw, group in seed_items(profile):
            config["keywords"][group]["items"].append(
                {"keyword": kw, "source": "seed", "added_date": today})

    existing = {normalize(i["keyword"])
                for g in config["keywords"].values() for i in g["items"]}
    to_add = []
    for c in candidates:
        if c.get("status") != "approved":
            continue
        norm = normalize(c["keyword"])
        if norm in existing:
            continue
        existing.add(norm)
        group = classify(c["keyword"], profile)
        to_add.append((group, {"keyword": c["keyword"], "source": c.get("source", "seed"),
                               "added_date": today, "level": c.get("level")}))

    rejected = [c for c in candidates if c.get("status") == "rejected"]

    for group, item in to_add:
        print(f"[{group}] + {item['keyword']} (source={item['source']}, level={item.get('level')})")
    print(f"\n将新增 {len(to_add)} 条，归档 rejected {len(rejected)} 条")
    if args.dry_run:
        print("（dry-run，未写入任何文件）")
        return

    for group, item in to_add:
        config["keywords"][group]["items"].append(item)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    if rejected:
        archive_path = ROOT / "keyword_engine" / "archive_rejected.yaml"
        archive = yaml.safe_load(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else []
        archive.extend({"keyword": c["keyword"], "rejected_date": today,
                        "reason": c.get("reason", "")} for c in rejected)
        archive_path.write_text(yaml.dump(archive, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"已写入 {config_path}")


if __name__ == "__main__":
    main()
