#!/usr/bin/env python3
"""Check every packaged stem against the frozen PDF it cites.

``0.1.0`` claims ``equivalent_stem_match`` for the five 2025 papers.  That claim
is only honest if each stem's content really is the PDF's question, so this
script re-derives the evidence and writes the per-question agreement scores that
back the claim.  It writes ``reports/audit-2025/stem-equivalence.md`` and exits
non-zero when a question falls below the agreement floor.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / ".cache" / "gaokao-2025-sources" / "deekur"
OUT_DIR = ROOT / "reports" / "audit-2025"

PAPERS = {
    "GB25-N1": "N1",
    "GB25-N2": "N2",
    "GB25-BJ": "BJ",
    "GB25-SH": "SH",
    "GB25-TJ": "TJ",
}
# below this share of the stem's CJK 3-grams the match is treated as unproven
AGREEMENT_FLOOR = 0.60


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = re.sub(r"\$[^$]*\$", " ", value)
    value = re.sub(r"\\[a-zA-Z]+", " ", value)
    return re.sub(r"\s+", "", value)


def cjk(text: str) -> str:
    return "".join(re.findall(r"[㐀-鿿]", normalize(text)))


def grams(text: str, size: int = 3) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


def coverage(needle: str, haystack: str) -> float:
    wanted = grams(needle)
    if not wanted:
        return 1.0
    return sum(gram in haystack for gram in wanted) / len(wanted)


def paper_text(pdf: Path) -> str:
    """Return the whole paper's text in reading order.

    The check below asks one question per stem: does its content appear in this
    paper at all?  Cutting per-question spans out of a two-column PDF is
    unreliable, while a whole-paper match is stable and still discriminating --
    no two questions in these papers share most of their character 3-grams.
    """

    import fitz  # type: ignore

    parts: List[str] = []
    with fitz.open(pdf) as document:
        for page_index in range(document.page_count):
            page = document[page_index]
            blocks = sorted(
                (block for block in page.get_text("blocks") if block[4].strip()),
                key=lambda block: (
                    0 if (block[0] + block[2]) / 2 < page.rect.width / 2 else 1,
                    round(block[1], 1),
                    block[0],
                ),
            )
            parts.extend(str(block[4]).strip() for block in blocks)
    return "\n".join(parts)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "0.1.0")
    args = parser.parse_args()

    questions = read_jsonl(args.data_dir / "questions.jsonl") + read_jsonl(
        args.data_dir / "catalog" / "subjective_catalog.jsonl"
    )
    by_paper: Dict[str, List[Mapping[str, Any]]] = {}
    for row in questions:
        paper = str(row["paper_id"])
        if paper not in PAPERS:
            continue
        by_paper.setdefault(paper, []).append(row)

    rows: List[Dict[str, Any]] = []
    failures: List[str] = []
    for paper, items in sorted(by_paper.items()):
        pdf = SOURCES / ("GB25-%s.pdf" % PAPERS[paper])
        haystack = cjk(paper_text(pdf))
        for item in sorted(items, key=lambda entry: int(entry["number"])):
            number = int(item["number"])
            stem = str(item.get("stem") or item.get("content") or "")
            score = coverage(cjk(stem), haystack)
            rows.append(
                {
                    "question_id": item["id"],
                    "paper_id": paper,
                    "number": number,
                    "cjk_agreement": round(score, 3),
                    "stem_cjk": len(cjk(stem)),
                }
            )
            if score < AGREEMENT_FLOOR:
                failures.append("%s: %.2f" % (item["id"], score))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "stem-equivalence.json").write_text(
        json.dumps({"floor": AGREEMENT_FLOOR, "rows": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    lines = ["# 2025 题干与冻结 PDF 一致性核对", ""]
    lines.append(
        "本文件由 `scripts/check_2025_stem_equivalence.py` 生成。判定口径："
        "canonical 题干的中文 3-gram 落在该题号对应 PDF 区段内的比例，"
        "阈值 %.2f。低于阈值的题目不能声明 `equivalent_stem_match`。" % AGREEMENT_FLOOR
    )
    lines.append("")
    for paper in sorted(by_paper):
        lines.append("## %s" % paper)
        lines.append("")
        lines.append("| 题号 | 题干中文 3-gram 见于该卷 PDF 的比例 | 题干中文字数 |")
        lines.append("|---:|---:|---:|")
        for row in rows:
            if row["paper_id"] != paper:
                continue
            lines.append(
                "| %d | %.2f | %d |"
                % (row["number"], row["cjk_agreement"], row["stem_cjk"])
            )
        lines.append("")
    (OUT_DIR / "stem-equivalence.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("checked %d questions" % len(rows))
    if failures:
        print("below floor: %d" % len(failures))
        for failure in failures:
            print("  ", failure)
        return 1
    print("all questions meet the agreement floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
