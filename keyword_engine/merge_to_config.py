"""Phase 6：把 approved 候选词合并进 config/keyword_config.yaml。

规则：
- 只合并 status: approved 的词条；首次运行时先把科研画像词条写入作种子词
  （纯字符串形式 = 默认 weight 2 + locked，权重最高、不随负反馈降权）；
- 候选词分组：候选若显式带 group 字段且存在于 config 则采用，否则按画像
  类别归一化匹配（species→species，methods/tools→methods），匹配不到归 concept；
- 扩展词条写为 {keyword, weight: 1, locked: false}（权重较低、可随负反馈降权）；
- level: exclude 的 approved 候选合并进 config 的 negative 排除词列表（去重）；
- status: rejected 的词条归档到 keyword_engine/archive_rejected.yaml，
  避免下次扩充重复推荐；
- 写回统一用 feedback.learning.dump_config_with_header 的规范格式；
- --dry-run 只预览不写入。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from feedback.learning import dump_config_with_header  # noqa: E402
from keyword_engine.expand_keywords import normalize  # noqa: E402
from processing.keyword_filter import item_keyword  # noqa: E402

GROUPS = {"core": 15, "core_broad": 15, "concept": 10,
          "methods": 8, "species": 5, "topics": 2}  # 权重由用户确认，勿擅自修改


def classify(keyword: str, profile: dict) -> str:
    """按画像类别匹配分组（methods/tools 画像词都归 methods），匹配不到归入 concept。"""
    norm = normalize(keyword)
    for keys, group in ((("species",), "species"), (("methods", "tools"), "methods")):
        for key in keys:
            for item in profile.get(key) or []:
                n = normalize(item)
                if n and (n in norm or norm in n):
                    return group
    return "concept"


def seed_items(profile: dict) -> list:
    """画像词条转为种子项：species 归 species，methods/tools 归 methods，其余归 concept。"""
    items = []
    for key, group in (("species", "species"), ("methods", "methods"), ("tools", "methods")):
        for kw in profile.get(key) or []:
            items.append((str(kw), group))
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
        # 首次运行：用画像词条初始化（纯字符串 = 默认 weight 2 + locked）
        config = {"keywords": {g: {"weight": w, "items": []} for g, w in GROUPS.items()},
                  "negative": profile.get("exclude") or []}
        for kw, group in seed_items(profile):
            config["keywords"][group]["items"].append(kw)

    existing = {normalize(item_keyword(i))
                for g in config["keywords"].values() for i in g["items"]}
    existing_negative = {normalize(str(w)) for w in (config.get("negative") or [])}
    to_add = []
    to_exclude = []  # level: exclude 的 approved 候选 → 进 config 的 negative 排除词列表
    for c in candidates:
        if c.get("status") != "approved":
            continue
        norm = normalize(c["keyword"])
        if c.get("level") == "exclude":
            if norm not in existing_negative:
                existing_negative.add(norm)
                to_exclude.append(str(c["keyword"]))
            continue
        if norm in existing:
            continue
        existing.add(norm)
        group = c.get("group") if c.get("group") in config["keywords"] else classify(c["keyword"], profile)
        # 扩展词必须显式 locked: false——新格式下缺省为锁定，扩展词要可随负反馈降权
        to_add.append((group, {"keyword": c["keyword"], "weight": 1, "locked": False}))

    rejected = [c for c in candidates if c.get("status") == "rejected"]

    for group, item in to_add:
        print(f"[{group}] + {item['keyword']}")
    for kw in to_exclude:
        print(f"[negative] + {kw}（排除词）")
    print(f"\n将新增 {len(to_add)} 条、排除词 {len(to_exclude)} 条，归档 rejected {len(rejected)} 条")
    if args.dry_run:
        print("（dry-run，未写入任何文件）")
        return

    for group, item in to_add:
        config["keywords"][group]["items"].append(item)
    if to_exclude:
        config["negative"] = (config.get("negative") or []) + to_exclude
    config_path.parent.mkdir(parents=True, exist_ok=True)
    dump_config_with_header(config, config_path)

    if rejected:
        archive_path = ROOT / "keyword_engine" / "archive_rejected.yaml"
        archive = yaml.safe_load(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else []
        archive.extend({"keyword": c["keyword"], "rejected_date": today,
                        "reason": c.get("reason", "")} for c in rejected)
        archive_path.write_text(yaml.dump(archive, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"已写入 {config_path}")


if __name__ == "__main__":
    main()
