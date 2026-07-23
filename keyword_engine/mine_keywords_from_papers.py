"""Phase 5：从参考文献二次挖掘关键词。

读取参考论文 PDF（只提取前两页文本，即标题+摘要，不解析全文/图片/补充材料）
→ 拼装 Prompt → 调用 AI → 去重截断 → 追加到 output_candidates.yaml（status: pending），
同样必须经过 review_candidates.py 人工确认后才能入库。
"""
import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai.ai_client import call_model  # noqa: E402
from keyword_engine.expand_keywords import (  # noqa: E402
    LEVELS, collect_input_keywords, load_existing_keywords, normalize)

MAX_CHARS_PER_PAPER = 1500


def extract_pdf_abstract(path: Path, pages: int = 2) -> str:
    """提取 PDF 前 2 页文本并压缩空白，截断到 MAX_CHARS_PER_PAPER。"""
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    text = "\n".join((reader.pages[i].extract_text() or "")
                     for i in range(min(pages, len(reader.pages))))
    return " ".join(text.split())[:MAX_CHARS_PER_PAPER]


def collect_papers(input_dir: Path) -> list:
    """收集 PDF（跳过补充材料文件），返回 [Path]。"""
    pdfs = []
    for p in sorted(input_dir.glob("*.pdf")):
        if "supplement" in p.name.lower():
            continue
        pdfs.append(p)
    return pdfs


def main():
    ap = argparse.ArgumentParser(description="从参考文献二次挖掘关键词")
    ap.add_argument("--input", required=True, help="参考论文目录（PDF）")
    ap.add_argument("--profile", required=True, help="科研画像 YAML")
    ap.add_argument("--existing-keywords", default=str(ROOT / "config" / "keyword_config.yaml"))
    ap.add_argument("--output", required=True, help="候选词 YAML（追加写入）")
    ap.add_argument("--max-new", type=int, default=20)
    args = ap.parse_args()

    profile = yaml.safe_load(Path(args.profile).read_text(encoding="utf-8"))
    pdfs = collect_papers(Path(args.input))
    if not pdfs:
        print("未找到可用 PDF。")
        return

    blocks = []
    for i, pdf in enumerate(pdfs, 1):
        try:
            abstract = extract_pdf_abstract(pdf)
        except Exception as e:  # noqa: BLE001
            print(f"[跳过] {pdf.name}: 解析失败 {e}")
            continue
        blocks.append(f"【论文 {i}】{pdf.name}\n{abstract}")
        print(f"[{i}] {pdf.name} -> 提取 {len(abstract)} 字符")
    if not blocks:
        print("所有 PDF 解析失败。")
        return

    template = (ROOT / "prompts" / "mine_from_paper_prompt.txt").read_text(encoding="utf-8")
    prompt = (template
              .replace("{{profile}}", yaml.dump(profile, allow_unicode=True, sort_keys=False))
              .replace("{{papers}}", "\n\n".join(blocks))
              .replace("{{n}}", str(len(blocks)))
              .replace("{{max_new}}", str(args.max_new)))

    result = call_model(prompt, response_format="json")
    reasoning = result.get("reasoning") or {}
    source_paper = result.get("source_paper") or {}

    seen = collect_input_keywords(profile) | load_existing_keywords(Path(args.existing_keywords))
    out_path = Path(args.output)
    existing_candidates = yaml.safe_load(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    for c in existing_candidates or []:
        seen.add(normalize(c["keyword"]))

    new_candidates = []
    for field, level in LEVELS:
        for kw in result.get(field) or []:
            norm = normalize(kw)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            idx = source_paper.get(kw)
            src = f"paper:{pdfs[idx - 1].name}" if isinstance(idx, int) and 1 <= idx <= len(pdfs) else "paper:unknown"
            new_candidates.append({
                "keyword": str(kw),
                "level": level,
                "source": src,
                "reason": reasoning.get(kw, ""),
                "status": "pending",
            })
            if len(new_candidates) >= args.max_new:
                break
        if len(new_candidates) >= args.max_new:
            break

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.dump((existing_candidates or []) + new_candidates,
                                  allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"新增候选 {len(new_candidates)} 条（上限 {args.max_new}），已追加到 {out_path}")


if __name__ == "__main__":
    main()
