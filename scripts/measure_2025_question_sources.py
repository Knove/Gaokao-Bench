#!/usr/bin/env python3
"""Measure how many independent publisher groups carry each 2025 question.

``strict_dual_source_v4`` asks for nine independent stem groups and six
independent joint groups per question.  This script measures what the frozen
2025 material actually supports, publisher by publisher, so the release can
record a real per-question evidence level instead of asserting one.

Only artifacts with an extractable text layer are used: the archived document
PDFs and preview pages.  Image-only answer pages are excluded from the stem
count by construction -- they cannot prove a stem without OCR -- and are counted
separately as answer-only corroboration.

Output: ``reports/audit-2025/question-source-groups.json``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / ".cache" / "gaokao-2025-sources"
CORPUS = ROOT / ".tmp_audit" / "agents" / "sartre" / "work" / "corpus.json"
OUT = ROOT / "reports" / "audit-2025" / "question-source-groups.json"

STEM_FLOOR = 0.55
ANSWER_MARKERS = ("【答案】", "参考答案", "答案：", "【解析】", "答案及解析", "答案解析")

# publisher -> independence group.  One publisher is one group no matter how
# many documents or uploaders it has.
PUBLISHER_GROUP = {
    "齐齐文库": "qiqiwenku",
    "人人文库": "rendoc",
    "360文库聚合": "360wenku",
    "360文库": "360wenku",
    "自主招生在线": "zizzs",
    "deekur/gaokaomath": "deekur",
    "搜狐": "sohu",
    "无忧考网": "51test",
    "百度文库": "baidu-wenku",
    "中国教育在线": "eol",
    "原创力文档": "book118",
    "道客巴巴": "doc88",
}

PAPERS = {"GB25-N1": "n1", "GB25-N2": "n2", "GB25-BJ": "bj", "GB25-SH": "sh", "GB25-TJ": "tj"}


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


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


def pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=180
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def documents() -> List[Dict[str, Any]]:
    """Return every text-extractable 2025 artifact with its publisher group."""

    documents: List[Dict[str, Any]] = []
    seen: set[str] = set()

    corpus = json.loads(CORPUS.read_text(encoding="utf-8")) if CORPUS.is_file() else []
    for row in corpus:
        publisher = str(row.get("publisher") or "")
        group = PUBLISHER_GROUP.get(publisher)
        if not group:
            continue
        text = str(row.get("text") or "")
        if len(cjk(text)) < 200:
            continue
        source_id = str(row.get("source_id") or "")
        if source_id in seen:
            continue
        seen.add(source_id)
        documents.append(
            {
                "source_id": source_id,
                "group": group,
                "publisher": publisher,
                "url": str(row.get("url") or ""),
                "artifact": str(row.get("artifact") or ""),
                "sha256": str(row.get("sha256") or ""),
                "text": text,
            }
        )

    # archived document PDFs that the corpus did not carry a text field for
    for pdf in sorted((SOURCE_ROOT / "qiqi-docs").glob("*.pdf")):
        source_id = "qiqi-" + pdf.stem
        if source_id in seen:
            continue
        text = pdf_text(pdf)
        if len(cjk(text)) < 200:
            continue
        meta_path = pdf.with_suffix(".json")
        url = ""
        if meta_path.is_file():
            url = str(json.loads(meta_path.read_text(encoding="utf-8")).get("url") or "")
        seen.add(source_id)
        documents.append(
            {
                "source_id": source_id,
                "group": "qiqiwenku",
                "publisher": "齐齐文库",
                "url": url,
                "artifact": str(pdf.relative_to(ROOT)),
                "sha256": sha(pdf),
                "text": text,
            }
        )

    baidu_meta: Dict[str, str] = {}
    baidu_index = SOURCE_ROOT / "baidu-wenku" / "meta.json"
    if baidu_index.is_file():
        for row in json.loads(baidu_index.read_text(encoding="utf-8")):
            baidu_meta[str(row.get("paper") or "").lower()] = str(row.get("url") or "")

    for pdf in sorted((SOURCE_ROOT / "baidu-wenku").glob("*.pdf")):
        source_id = "baidu-wenku-" + pdf.stem.lower()
        if source_id in seen:
            continue
        text = pdf_text(pdf)
        if len(cjk(text)) < 200:
            continue
        seen.add(source_id)
        documents.append(
            {
                "source_id": source_id,
                "group": "baidu-wenku",
                "publisher": "百度文库",
                "url": baidu_meta.get(pdf.stem.lower(), ""),
                "artifact": str(pdf.relative_to(ROOT)),
                "sha256": sha(pdf),
                "text": text,
            }
        )
    return documents


def has_answer_section(text: str) -> bool:
    return any(marker in (text or "") for marker in ANSWER_MARKERS)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "0.1.0")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    questions = read_jsonl(args.data_dir / "questions.jsonl") + read_jsonl(
        args.data_dir / "catalog" / "subjective_catalog.jsonl"
    )
    docs = documents()
    for doc in docs:
        doc["cjk"] = cjk(doc["text"])
        doc["has_answers"] = has_answer_section(doc["text"])

    result: Dict[str, Any] = {
        "schema_version": "1.0",
        "stem_floor": STEM_FLOOR,
        "documents": [
            {
                "source_id": doc["source_id"],
                "group": doc["group"],
                "publisher": doc["publisher"],
                "artifact": doc["artifact"],
                "sha256": doc["sha256"],
                "url": doc["url"],
                "cjk_chars": len(doc["cjk"]),
                "has_answer_section": doc["has_answers"],
            }
            for doc in docs
        ],
        "questions": {},
    }

    for item in questions:
        paper_id = str(item.get("paper_id") or "")
        if paper_id not in PAPERS:
            continue
        question_id = str(item.get("id") or item.get("question_id"))
        stem_cjk = cjk(str(item.get("stem") or item.get("content") or ""))
        stem_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        joint_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for doc in docs:
            score = coverage(stem_cjk, doc["cjk"])
            if score < STEM_FLOOR:
                continue
            record = {"source_id": doc["source_id"], "agreement": round(score, 3)}
            stem_groups[doc["group"]].append(record)
            if doc["has_answers"]:
                joint_groups[doc["group"]].append(record)
        result["questions"][question_id] = {
            "paper_id": paper_id,
            "number": int(item["number"]),
            "stem_groups": {group: sorted(rows, key=lambda row: -row["agreement"]) for group, rows in sorted(stem_groups.items())},
            "joint_groups": {group: sorted(rows, key=lambda row: -row["agreement"]) for group, rows in sorted(joint_groups.items())},
            "stem_group_count": len(stem_groups),
            "joint_group_count": len(joint_groups),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print("documents usable:", len(docs))
    by_paper: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in result["questions"].values():
        by_paper[entry["paper_id"]].append(entry)
    for paper in sorted(by_paper):
        rows = by_paper[paper]
        stem_counts = sorted(row["stem_group_count"] for row in rows)
        joint_counts = sorted(row["joint_group_count"] for row in rows)
        print(
            "%s questions=%d stem_groups min=%d median=%d max=%d | joint min=%d median=%d max=%d"
            % (
                paper,
                len(rows),
                stem_counts[0],
                stem_counts[len(stem_counts) // 2],
                stem_counts[-1],
                joint_counts[0],
                joint_counts[len(joint_counts) // 2],
                joint_counts[-1],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
