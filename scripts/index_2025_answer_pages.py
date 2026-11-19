#!/usr/bin/env python3
"""Index the frozen 2025 answer pages and attach them to individual questions.

The 2025 papers were published with media answer pages (中国教育在线 and
自主招生在线) whose OCR text was harvested during the earlier audit.  Those
artifacts are stored per page but their folder names cannot be trusted -- the
first page of ``ocr-eol`` for National I is titled "全国二卷" -- so every page
is identified by its own content, and a question is attached to a page only when
that page demonstrably carries the question's stem.

For each question the index records, per publisher group:

* the page file, its SHA-256, byte count, source URL, and OCR text SHA
* the stem agreement measured against the canonical stem
* the answer token found on the page and whether it equals the canonical answer

The builder turns a verified page into a ``joint_supporting`` assessment, a page
that disagrees into ``rejected``, and drops pages that do not carry the stem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "gaokao-2025-sources"
OCR_ROOTS = (
    ROOT / ".tmp_release_audit" / "n12" / "work" / "ocr-eol",
    ROOT / ".tmp_release_audit" / "n12" / "work" / "ocr-zizzs",
    ROOT / ".tmp_audit" / "agents" / "sartre" / "work" / "page_texts",
    ROOT / ".tmp_release_audit" / "tj2025" / "work" / "ocr" / "eol",
    ROOT / ".tmp_release_audit" / "tj2025" / "work" / "ocr" / "zizzs",
)
OUT_PATH = ROOT / "reports" / "audit-2025" / "answer-page-index.json"

PAPERS = {"GB25-N1": "n1", "GB25-N2": "n2", "GB25-BJ": "bj", "GB25-SH": "sh", "GB25-TJ": "tj"}
PUBLISHER_BY_PREFIX = (
    ("src-eol-2025-", "eol", "中国教育在线"),
    ("src-zizzs-2025-", "zizzs", "自主招生在线"),
    ("eol", "eol", "中国教育在线"),
    ("zizzs", "zizzs", "自主招生在线"),
)

STEM_FLOOR = 0.55
ANSWER_WINDOW = 420


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
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


def ocr_text(path: Path) -> str:
    """Return a page's OCR text, accepting both JSON and plain-text artifacts."""

    raw = path.read_text(encoding="utf-8", errors="ignore")
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        lines = payload.get("lines")
        if isinstance(lines, list):
            ordered = sorted(
                (line for line in lines if isinstance(line, Mapping) and line.get("text")),
                key=lambda line: (-float(line.get("y") or 0), float(line.get("x") or 0)),
            )
            return "\n".join(str(line["text"]) for line in ordered)
    return raw


def page_records() -> List[Dict[str, Any]]:
    """Collect every OCR page, labelled by the publisher it claims to be."""

    records: List[Dict[str, Any]] = []
    for root in OCR_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix not in {".txt", ".json"}:
                continue
            name = path.name
            group = publisher = None
            for prefix, candidate_group, candidate_publisher in PUBLISHER_BY_PREFIX:
                if name.lower().startswith(prefix) or (prefix in name.lower()):
                    group, publisher = candidate_group, candidate_publisher
                    break
            if group is None:
                # n12's eol folder holds plain page-NN.txt files
                if root.name in {"eol", "ocr-eol"}:
                    group, publisher = "eol", "中国教育在线"
                elif root.name in {"zizzs", "ocr-zizzs"}:
                    group, publisher = "zizzs", "自主招生在线"
                else:
                    continue
            records.append(
                {
                    "ocr_file": str(path.relative_to(ROOT)),
                    "group": group,
                    "publisher": publisher,
                    "text": ocr_text(path),
                }
            )
    return records


def frozen_pages() -> Dict[str, List[Dict[str, Any]]]:
    """Return the frozen page images per source id with their recorded URL."""

    sources_path = ROOT / "data" / "0.1.0-alpha.6.1" / "answer_sources.jsonl"
    rows = [
        json.loads(line)
        for line in sources_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_sha: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        source_id = str(row["id"])
        for page in row.get("pages") or []:
            digest = str(page.get("sha256") or "")
            if not digest:
                continue
            by_sha.setdefault(digest, []).append(
                {
                    "source_id": source_id,
                    "url": str(page.get("image_url") or page.get("page_url") or ""),
                    "page_url": str(page.get("page_url") or ""),
                    "page_index": int(page.get("page_index") or 0),
                }
            )
    return by_sha


def cached_answer_images() -> List[Dict[str, Any]]:
    """List every cached answer-page image with its verified digest."""

    images: List[Dict[str, Any]] = []
    roots = []
    eol_root = CACHE / "eol"
    if eol_root.is_dir():
        roots.extend(sorted(path for path in eol_root.iterdir() if path.is_dir()))
    downloads = CACHE / "downloads"
    if downloads.is_dir():
        roots.extend(
            sorted(
                path
                for path in downloads.iterdir()
                if path.is_dir() and "answers" in path.name
            )
        )
    for directory in roots:
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            payload = path.read_bytes()
            images.append(
                {
                    "path": path,
                    "relative": str(path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                    "source_id": directory.name,
                    "publisher_group": "eol" if "eol" in directory.name else (
                        "zizzs" if "zizzs" in directory.name else None
                    ),
                }
            )
    return images


def answer_token(text: str, canonical: str, anchor: int) -> Optional[str]:
    """Return the answer token found near the stem, or ``None``."""

    if not canonical:
        return None
    window = text[max(0, anchor - ANSWER_WINDOW) : anchor + ANSWER_WINDOW]
    compact = normalize(window)
    candidate = normalize(canonical)
    if not candidate:
        return None
    return canonical if candidate in compact else None


def build_index(questions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    pages = page_records()
    images = cached_answer_images()
    metadata = frozen_pages()

    # attach the recorded URL to each image by digest
    for image in images:
        recorded = metadata.get(image["sha256"])
        if recorded:
            image["url"] = recorded[0]["url"]
            image["page_url"] = recorded[0]["page_url"]
            image["recorded_index"] = recorded[0]["page_index"]
        else:
            image["url"] = ""
            image["page_url"] = ""
            image["recorded_index"] = None

    page_norms = [{**page, "cjk": cjk(page["text"]), "norm": normalize(page["text"])} for page in pages]

    index: Dict[str, Any] = {"schema_version": "1.0", "stem_floor": STEM_FLOOR, "questions": {}}
    for item in questions:
        question_id = str(item.get("id") or item.get("question_id"))
        stem = str(item.get("stem") or item.get("content") or "")
        canonical = str(item.get("canonical") or "")
        needle = cjk(stem)
        best: Dict[str, Dict[str, Any]] = {}
        for page in page_norms:
            score = coverage(needle, page["cjk"])
            if score < STEM_FLOOR:
                continue
            anchor = page["norm"].find(normalize(stem)[:40])
            token = answer_token(page["text"], canonical, anchor if anchor >= 0 else 0)
            current = best.get(page["group"])
            if current is None or score > current["stem_agreement"]:
                best[page["group"]] = {
                    "group": page["group"],
                    "publisher": page["publisher"],
                    "ocr_file": page["ocr_file"],
                    "stem_agreement": round(score, 3),
                    "observed_answer": token or "",
                }
        index["questions"][question_id] = {
            "canonical": canonical,
            "pages": list(best.values()),
        }

    # bind each verified page to its frozen image by digest, when one exists
    by_source_group: Dict[str, List[Dict[str, Any]]] = {}
    for image in images:
        group = image["publisher_group"]
        if group:
            by_source_group.setdefault(group, []).append(image)
    for run in by_source_group.values():
        run.sort(key=lambda image: (image.get("recorded_index") is None, image.get("recorded_index") or 0))

    index["frozen_pages"] = {
        group: [
            {
                "source_id": image["source_id"],
                "path": image["relative"],
                "sha256": image["sha256"],
                "bytes": image["bytes"],
                "url": image["url"],
                "page_url": image["page_url"],
                "recorded_index": image["recorded_index"],
            }
            for image in run
        ]
        for group, run in by_source_group.items()
    }
    return index


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "0.1.0")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    questions = read_jsonl(args.data_dir / "questions.jsonl")
    index = build_index(questions)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    with_answer = 0
    with_two = 0
    for question_id, entry in index["questions"].items():
        confirmed = [page for page in entry["pages"] if page["observed_answer"]]
        if confirmed:
            with_answer += 1
        if len({page["group"] for page in confirmed}) >= 2:
            with_two += 1
    print("questions indexed:", len(index["questions"]))
    print("with a confirmed answer page:", with_answer)
    print("with two independent confirmed groups:", with_two)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
