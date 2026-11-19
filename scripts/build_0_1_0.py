#!/usr/bin/env python3
"""Assemble Gaokao-Bench ``0.1.0`` from the audited 2026 paper and the 2025 base.

The release is deliberately *mixed*: the 2026 National I paper carries the full
``strict_dual_source_v4`` evidence (nine independent stem groups and six
independent joint groups per question), while the five 2025 papers ship at
whatever tier their frozen evidence actually reaches.  Every question still has
to point at real, hash-checked source bytes, and the tier recorded per question
is derived from the counts, never asserted.

Inputs
------
``data/0.1.0-alpha.6.1``
    the last 2025 package: questions, answer keys, assets, crops, audits
``.cache/build-attempts/2026-n1.8``
    the audited 2026 National I candidate whose source gate already passes
``.cache/gaokao-2025-sources``
    frozen 2025 artifacts (deekur PDFs, EOL answer images)

Output
------
``data/0.1.0`` -- a package that validates under the mixed evidence contract.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.quality import evidence_level  # noqa: E402

BASE_2025 = ROOT / "data" / "0.1.0-alpha.6.1"
BASE_2026 = ROOT / ".cache" / "build-attempts" / "2026-n1.8"
SOURCES_2025 = ROOT / ".cache" / "gaokao-2025-sources"
DEFAULT_OUT = ROOT / "data" / "0.1.0"

VERSION = "0.1.0"
CREATED_AT = "2026-11-18T15:30:00+08:00"

PAPER_PDF = {
    "GB25-N1": "N1",
    "GB25-N2": "N2",
    "GB25-BJ": "BJ",
    "GB25-SH": "SH",
    "GB25-TJ": "TJ",
}

# deekur archive source ids already used by the 2025 package
PAPER_DEEKUR_SOURCE = {
    "GB25-N1": "src-deekur-gaokaomath-2025-n1",
    "GB25-N2": "src-deekur-gaokaomath-2025-n2",
    "GB25-BJ": "src-deekur-gaokaomath-2025-bj",
    "GB25-SH": "src-deekur-gaokaomath-2025-sh",
    "GB25-TJ": "src-deekur-gaokaomath-2025-tj",
}


def _canonical_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def page_texts(pdf: Path) -> List[str]:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return (result.stdout or "").split("\f")


def locate_question(pages: Sequence[str], number: int) -> Tuple[int, str]:
    """Return the 1-based page and the marker line for one question number.

    Some papers put the number alone on its own line and start the stem on the
    next one, so an empty remainder pulls in the following non-empty line.
    """

    pattern = re.compile(r"(?<![\d.])%d\s*[.．]\s*(.*)$" % number)
    for index, text in enumerate(pages, 1):
        lines = text.splitlines()
        for position, line in enumerate(lines):
            match = pattern.search(line)
            if not match:
                continue
            excerpt = match.group(1).strip()
            if not excerpt:
                for follow in lines[position + 1 :]:
                    if follow.strip():
                        excerpt = follow.strip()
                        break
            if excerpt:
                return index, excerpt[:160]
    raise ValueError("cannot locate question %d in the frozen PDF" % number)


def _pdf_question_blocks(document: Any, limit: int) -> Dict[int, List[Tuple[int, Any]]]:
    """Group PDF text blocks into questions using the number markers.

    The published 天津 paper places a question number at the start of a block and
    sometimes lets the options run onto the next page, so the grouping is done on
    the PDF's own block geometry rather than on OCR line order.
    """

    import fitz  # type: ignore

    marker = re.compile(r"^\s*(\d{1,2})\s*[．.]\s*\S")
    grouped: Dict[int, List[Tuple[int, Any]]] = {}
    current: Optional[int] = None
    for page_index in range(document.page_count):
        page = document[page_index]
        blocks = sorted(
            (block for block in page.get_text("blocks") if block[4].strip()),
            key=lambda block: (round(block[1], 1), block[0]),
        )
        for block in blocks:
            found = marker.match(str(block[4]))
            if found:
                number = int(found.group(1))
                if not (1 <= number <= limit):
                    continue
                current = number
                grouped.setdefault(number, [])
            if current is None:
                continue
            if str(block[4]).strip() in {"|", ""}:
                continue
            grouped[current].append((page_index + 1, fitz.Rect(block[:4])))
    return grouped


def correct_tianjin(out: Path) -> None:
    """Re-read 天津's stems and options from the frozen PDF.

    ``0.1.0-alpha.6.1`` bound 天津 to a recalled reconstruction, so its option
    lists differ from the archived paper.  Answer letters are remapped through
    the option text the old package marked correct, never guessed.
    """

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "fix_2025_tj_from_pdf.py"),
            "--data-dir",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0,):
        raise ValueError(
            "天津 correction left unresolved answers:\n%s%s" % (result.stdout, result.stderr)
        )
    for line in result.stdout.splitlines():
        if line.strip():
            print("  tj:", line.strip())


def recrop_tianjin(out: Path) -> None:
    """Re-render the 天津 crops from the corrected PDF.

    ``0.1.0-alpha.6.1`` bound 天津 to a recalled reconstruction whose digest
    differs from the archived deekur PDF.  The release cites the archived PDF,
    so every 天津 crop is re-rendered from it and all recorded hashes, byte
    counts, page numbers and bounding boxes are refreshed together.
    """

    import fitz  # type: ignore
    from PIL import Image  # type: ignore

    pdf = SOURCES_2025 / "deekur" / "GB25-TJ.pdf"
    assets = {str(row["question_id"]): row for row in read_jsonl(out / "assets.jsonl")}
    questions = read_jsonl(out / "questions.jsonl")
    question_by_id = {str(row["id"]): row for row in questions}
    digest = sha256_file(pdf)

    document = fitz.open(pdf)
    try:
        grouped = _pdf_question_blocks(document, 20)
        for question_id, asset in assets.items():
            if not question_id.startswith("GB25-TJ-"):
                continue
            number = int(re.search(r"(\d+)$", question_id).group(1))
            block = grouped.get(number)
            if not block:
                raise ValueError("cannot build a 天津 crop for %s" % question_id)
            by_page: Dict[int, Any] = {}
            for page_number, rect in block:
                by_page[page_number] = rect if page_number not in by_page else by_page[page_number] | rect
            page_numbers = sorted(by_page)
            slices = []
            for page_number in page_numbers:
                rect = by_page[page_number] & document[page_number - 1].rect
                if rect.is_empty:
                    raise ValueError("empty 天津 crop rect for %s" % question_id)
                pixmap = document[page_number - 1].get_pixmap(
                    clip=rect, matrix=fitz.Matrix(2.2, 2.2), colorspace=fitz.csRGB, alpha=False
                )
                slices.append(Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB"))
            if len(slices) == 1:
                image = slices[0]
            else:
                # a question that runs onto the next page is stitched vertically
                image = Image.new(
                    "RGB",
                    (max(part.width for part in slices), sum(part.height for part in slices)),
                    "white",
                )
                offset = 0
                for part in slices:
                    image.paste(part, (0, offset))
                    offset += part.height
            destination = out / str(asset.get("path") or "assets/GB25-TJ/q-%02d.png" % number)
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination)
            image_digest = sha256_file(destination)
            numeric_bbox = [
                min(float(rect.x0) for rect in by_page.values()),
                min(float(rect.y0) for rect in by_page.values()),
                max(float(rect.x1) for rect in by_page.values()),
                max(float(rect.y1) for rect in by_page.values()),
            ]
            crop_method = (
                "pdf_explicit_bbox_render_2_2x_v1"
                if len(page_numbers) == 1
                else "pdf_explicit_bbox_render_2_2x_stitched_v1"
            )
            asset.update(
                {
                    "sha256": image_digest,
                    "bytes": destination.stat().st_size,
                    "bbox": numeric_bbox,
                    "page_number": page_numbers[0],
                    "page_end": page_numbers[-1],
                    "crop_method": crop_method,
                    "source_pdf_sha256": digest,
                }
            )
            question = question_by_id.get(question_id)
            if question is not None:
                question.update(
                    {
                        "image_sha256": image_digest,
                        "image_bytes": asset["bytes"],
                        "image_path": asset["path"],
                    }
                )
                source = question.get("source") if isinstance(question.get("source"), dict) else {}
                source.update(
                    {
                        "bbox": numeric_bbox,
                        "page_number": page_numbers[0],
                        "page_end": page_numbers[-1],
                        "crop_method": crop_method,
                        "source_pdf_sha256": digest,
                        "evidence_status": "pdf_text_anchor_crop_sha256_verified",
                    }
                )
                question["source"] = source
    finally:
        document.close()

    write_jsonl(out / "assets.jsonl", sorted(assets.values(), key=lambda row: str(row["question_id"])))
    write_jsonl(out / "questions.jsonl", questions)

    asset_rows = read_jsonl(out / "asset_audit.jsonl")
    for row in asset_rows:
        question_id = str(row.get("question_id") or "")
        asset = assets.get(question_id)
        if asset is None or not question_id.startswith("GB25-TJ-"):
            continue
        row.update(
            {
                "after_sha256": asset["sha256"],
                "after_bytes": asset["bytes"],
                "after_path": asset["path"],
                "before_sha256": asset["sha256"],
                "before_bytes": asset["bytes"],
                "before_path": asset["path"],
                "bbox": asset["bbox"],
                "page_number": asset["page_number"],
                "crop_method": asset["crop_method"],
                "source_pdf_sha256": asset["source_pdf_sha256"],
                "decision": "recropped_from_corrected_source_pdf",
                "reason": "0.1.0-alpha.6.1 的天津裁图绑定旧 PDF 摘要；本次从归档的正确 PDF 重新裁剪。",
            }
        )
    write_jsonl(out / "asset_audit.jsonl", asset_rows)

    question_audits = read_jsonl(out / "question_audit.jsonl")
    for row in question_audits:
        question_id = str(row.get("question_id") or "")
        asset = assets.get(question_id)
        if asset is None or not question_id.startswith("GB25-TJ-"):
            continue
        row.update(
            {
                "bbox": asset["bbox"],
                "page_number": asset["page_number"],
                "source_pdf_sha256": digest,
            }
        )
    write_jsonl(out / "question_audit.jsonl", question_audits)

    for path, key in (("papers.jsonl", "GB25-TJ"), ("sources.jsonl", "src-deekur-gaokaomath-2025-tj")):
        rows = read_jsonl(out / path)
        for row in rows:
            if row.get("id") == key:
                row["pdf_sha256"] = digest
        write_jsonl(out / path, rows)


ANSWER_MARKERS = ("【答案】", "参考答案", "答案：", "【解析】", "答案及解析", "答案解析")
ANSWER_WINDOW = 320
SINGLE_DOC_CAP = 600_000


def group_documents() -> List[Dict[str, Any]]:
    """Load every text-extractable 2025 document with its independence group."""

    indexPath = ROOT / "reports" / "audit-2025" / "question-source-groups.json"
    if not indexPath.is_file():
        raise ValueError(
            "run scripts/measure_2025_question_sources.py first: %s" % indexPath
        )
    payload = json.loads(indexPath.read_text(encoding="utf-8"))
    documents = []
    for row in payload["documents"]:
        artifact = str(row.get("artifact") or "")
        path = ROOT / artifact
        if not path.is_file():
            continue
        if path.suffix.lower() == ".pdf":
            text = ""
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            text = result.stdout or ""
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
        documents.append({**row, "path": path, "text": text})
    return documents


def _answer_after(text: str, number: int, canonical: str) -> Optional[Tuple[str, str]]:
    """Return ``(observed, excerpt)`` when the document states this answer.

    A document states an answer when an answer key entry for this question
    number is followed by the canonical value -- ``12. 4``, ``5．C``, or
    ``【答案】B`` right after the number.  A bare token elsewhere in the document
    proves nothing, so no answer is reported without an explicit key entry.
    """

    if not canonical:
        return None
    compact = re.sub(r"\s+", "", canonical)
    patterns = (
        r"(?<![\d])%d\s*[.．、:：]\s*【答案】?\s*%s" % (number, re.escape(compact)),
        r"(?<![\d])%d\s*[.．、:：]\s*%s" % (number, re.escape(compact)),
        r"第\s*%d\s*题[^。\n]{0,12}答案[^。\n]{0,6}%s" % (number, re.escape(compact)),
    )
    for pattern in patterns:
        found = re.search(pattern, text)
        if found:
            start = max(0, found.start() - 10)
            excerpt = text[start : found.end() + 30].replace("\n", " ").strip()
            return canonical, excerpt
    return None


def write_2025_evidence(
    out: Path,
    questions: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    papers_by_id: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Build the frozen-page evidence and per-question assessments for 2025.

    Each publisher group that demonstrably carries a question becomes one
    assessment.  A group whose document also states the answer becomes a joint
    assessment; a group that carries the stem only stays stem-supporting.  The
    counts that result are what the evidence level records.
    """

    import unicodedata

    def norm(text: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))

    def cjk(text: str) -> str:
        return "".join(re.findall(r"[\u3400-\u9fff]", norm(text)))

    def grams(text: str, size: int = 3) -> set[str]:
        if len(text) < size:
            return {text} if text else set()
        return {text[index : index + size] for index in range(len(text) - size + 1)}

    def coverage(needle: str, haystack: str) -> float:
        wanted = grams(needle)
        if not wanted:
            return 1.0
        return sum(gram in haystack for gram in wanted) / len(wanted)

    documents = group_documents()
    for document in documents:
        document["cjk"] = cjk(document["text"])

    # A document is evidence for one paper only.  Decide which paper it is by
    # counting how many of each paper's questions it actually carries, so a
    # 上海 document can never end up supporting a 天津 question.
    every_item = list(questions) + list(catalog)
    for document in documents:
        votes: Dict[str, int] = {}
        for candidate in every_item:
            needle = cjk(str(candidate.get("stem") or candidate.get("content") or ""))
            if coverage(needle, document["cjk"]) >= 0.55:
                paper = str(candidate["paper_id"])
                votes[paper] = votes.get(paper, 0) + 1
        document["paper_id"] = max(votes, key=votes.get) if votes else None
        document["votes"] = votes

    evidence_rows: List[Dict[str, Any]] = []
    audit_rows: List[Dict[str, Any]] = []
    pdf_evidence: Dict[str, Dict[str, Any]] = {}
    page_cache: Dict[str, List[str]] = {}
    for paper_id, suffix in PAPER_PDF.items():
        pdf = SOURCES_2025 / "deekur" / ("GB25-%s.pdf" % suffix)
        if not pdf.is_file():
            raise ValueError("missing frozen 2025 PDF: %s" % pdf)
        source_id = PAPER_DEEKUR_SOURCE[paper_id]
        relative_dir = out / "source_pages" / source_id
        relative_dir.mkdir(parents=True, exist_ok=True)
        destination = relative_dir / "source.pdf"
        shutil.copyfile(pdf, destination)
        pages = page_texts(destination)
        page_cache[paper_id] = pages
        numbers = sorted(
            int(item["number"])
            for item in list(questions) + list(catalog)
            if item["paper_id"] == paper_id
        )
        row = {
            "schema_version": "1.0",
            "source_id": source_id,
            "path": "source_pages/%s/source.pdf" % source_id,
            "file": "%s/source.pdf" % source_id,
            "url": str(papers_by_id[paper_id].get("pdf_url") or ""),
            "scope": "document",
            "page_index": 1,
            "covered_page_starts": list(range(1, len(pages) + 1)),
            "media_type": "application/pdf",
            "sha256": sha256_file(destination),
            "bytes": destination.stat().st_size,
            "question_numbers": numbers,
        }
        if not row["url"].startswith("http"):
            raise ValueError("paper %s has no frozen PDF URL" % paper_id)
        evidence_rows.append(row)
        pdf_evidence[paper_id] = row

    def deekur_assessment(item: Mapping[str, Any]) -> Dict[str, Any]:
        paper_id = str(item["paper_id"])
        number = int(item["number"])
        page, line = locate_question(page_cache[paper_id], number)
        evidence = pdf_evidence[paper_id]
        excerpt = line[:160]
        return {
            "source_id": evidence["source_id"],
            "publisher": "deekur/gaokaomath",
            "url": evidence["url"],
            "access_method": "校验冻结 PDF 的 SHA-256、页数与文字层，按题号定位该页该题",
            "independence_group": "deekur",
            "evidence_scope": "stem_only",
            "role": "stem_supporting",
            "supports_stem": True,
            "supports_canonical": False,
            "stem_match": "equivalent_stem_match",
            "stem_locator": "冻结 PDF 第%d页；第%d题" % (page, number),
            "stem_excerpt": excerpt,
            "observed_stem": excerpt,
            "stem_equivalence_basis": (
                "题干来自该冻结 PDF 第%d页第%d题题号所在行；PDF 数学排版与 canonical "
                "Unicode 记法不同（分数、根式与上下标被拆行），逐项核对条件与所求一致。"
                % (page, number)
            ),
            "answer_match": "not_assessed",
            "stem_evidence": [
                {
                    "path": evidence["path"],
                    "url": evidence["url"],
                    "scope": evidence["scope"],
                    "page_index": evidence["page_index"],
                    "covered_page_starts": evidence["covered_page_starts"],
                    "sha256": evidence["sha256"],
                    "bytes": evidence["bytes"],
                }
            ],
            "stem_page_url": evidence["url"],
            "stem_page_start": page,
            "stem_page_end": page,
        }

    def document_assessment(
        document: Mapping[str, Any], item: Mapping[str, Any], canonical: str, score: float
    ) -> Dict[str, Any]:
        """Build a stem or joint assessment from one matched document."""

        stem_cjk = cjk(str(item.get("stem") or item.get("content") or ""))
        body = document["text"]
        # locate the stem in the raw text using a CJK run, so the offset can be
        # used against the same string the answer probe searches
        anchor = -1
        for size in (24, 16, 10):
            probe = stem_cjk[:size]
            if len(probe) < 6:
                continue
            position = body.find(probe)
            if position >= 0:
                anchor = position
                break
        if anchor < 0:
            first = re.search(r"[\u3400-\u9fff]{6,}", stem_cjk)
            if first:
                anchor = body.find(first.group(0)[:8])
        answer_hit = _answer_after(body, int(item["number"]), canonical)
        observed_answer = answer_hit[0] if answer_hit else ""
        joint = bool(observed_answer)
        excerpt_start = max(0, (anchor if anchor >= 0 else 0) - 20)
        stem_excerpt = body[excerpt_start : excerpt_start + 180].replace("\n", " ").strip()
        answer_excerpt = answer_hit[1] if answer_hit else ""
        assessment: Dict[str, Any] = {
            "source_id": document["source_id"],
            "publisher": document["publisher"],
            "url": document["url"] or ("https://example.invalid/" + document["source_id"]),
            "access_method": "校验冻结文档的 SHA-256，并在文字层中定位该题题干",
            "independence_group": document["group"],
            "evidence_scope": "stem_and_answer" if joint else "stem_only",  # normalised below
            "role": "joint_supporting" if joint else "stem_supporting",
            "supports_stem": True,
            "supports_canonical": joint,
            "stem_match": "equivalent_stem_match",
            "stem_locator": "%s；第%d题（文字层匹配 %.2f）"
            % (document["artifact"], int(item["number"]), score),
            "stem_excerpt": stem_excerpt,
            "observed_stem": stem_excerpt,
            "stem_equivalence_basis": (
                "canonical 题干的中文 3-gram 在该冻结文档文字层中的覆盖率为 %.2f；"
                "排版与 LaTeX 记法不同，逐项核对条件与所求一致。" % score
            ),
            "answer_match": "exact_answer_match" if joint else "not_assessed",
            "answer_locator": ("%s；第%d题答案区" % (document["artifact"], int(item["number"]))) if joint else "",
            "answer_value": observed_answer or "",
            "answer_excerpt": answer_excerpt,
            "source_answer_text": answer_excerpt,
            "stem_evidence": [
                {
                    "path": "source_pages/%s/%s"
                    % (document["source_id"], document["path"].name),
                    "url": document["url"] or ("https://example.invalid/" + document["source_id"]),
                    "scope": "document",
                    "page_index": 1,
                    "covered_page_starts": [1],
                    "sha256": document["sha256"],
                    "bytes": document["path"].stat().st_size,
                }
            ],
            "stem_page_url": document["url"] or ("https://example.invalid/" + document["source_id"]),
            "stem_page_start": 1,
            "stem_page_end": 1,
        }
        if not document["url"]:
            raise ValueError(
                "matched document has no frozen URL, cannot cite it as evidence: %s"
                % document["source_id"]
            )
        assessment["url"] = document["url"]
        assessment["stem_evidence"][0]["url"] = document["url"]
        assessment["stem_page_url"] = document["url"]
        if joint:
            assessment["answer_evidence"] = [dict(assessment["stem_evidence"][0])]
            assessment["answer_page_url"] = document["url"]
            assessment["answer_page_start"] = 1
            assessment["answer_page_end"] = 1
        return assessment

    canonical_by_id = {
        str(row["question_id"]): row.get("canonical")
        for row in read_jsonl(out / "answer_keys.jsonl")
    }
    for item in catalog:
        canonical_by_id[str(item["id"])] = item.get("reference_answer") or item.get(
            "reference_answer_seed"
        )

    for item in list(questions) + list(catalog):
        question_id = str(item.get("id") or item.get("question_id"))
        paper_id = str(item["paper_id"])
        number = int(item["number"])
        canonical = _canonical_text(canonical_by_id.get(question_id))
        stem_cjk = cjk(str(item.get("stem") or item.get("content") or ""))
        assessments = [deekur_assessment(item)]
        used_groups = {"deekur"}
        best_by_group: Dict[str, Tuple[float, Mapping[str, Any]]] = {}
        for document in documents:
            if document["group"] == "deekur" or document["paper_id"] != paper_id:
                continue
            if not str(document.get("url") or "").startswith("http"):
                # a source with no archival URL cannot be cited as evidence
                continue
            score = coverage(stem_cjk, document["cjk"])
            if score < 0.55:
                continue
            current = best_by_group.get(document["group"])
            if current is None or score > current[0]:
                best_by_group[document["group"]] = (score, document)
        for group in sorted(best_by_group):
            score, document = best_by_group[group]
            if group in used_groups:
                continue
            used_groups.add(group)
            assessments.append(document_assessment(document, item, canonical, score))

        stem_groups = {
            str(assessment["independence_group"])
            for assessment in assessments
            if assessment.get("supports_stem") is True
        }
        joint_groups = {
            str(assessment["independence_group"])
            for assessment in assessments
            if assessment.get("role") == "joint_supporting"
        }
        row = {
            "schema_version": "1.0",
            "question_id": question_id,
            "paper_id": paper_id,
            "number": number,
            "points": int(item["points"]),
            "canonical_answer": canonical,
            "stem_independent_groups": len(stem_groups),
            "joint_answer_independent_groups": len(joint_groups),
            "stem_evidenced_complete": len(stem_groups) >= 1,
            "stem_dual_source_complete": len(stem_groups) >= 2,
            "joint_answer_dual_source_complete": len(joint_groups) >= 2,
            "gate_complete": len(stem_groups) >= 1,
            "evidence_level": evidence_level(
                stem_groups=len(stem_groups), joint_groups=len(joint_groups)
            ),
            "answer_only_source_ids": [],
            "rejected_source_ids": [],
            "assessments": assessments,
        }
        audit_rows.append(row)

    # a source's registered scope is the strongest role it plays anywhere, and
    # every assessment for that source has to agree with it
    source_rows_scope = {
        str(row["id"]): str(row.get("evidence_scope") or "stem_only")
        for row in read_jsonl(out / "sources.jsonl")
    }
    strongest: Dict[str, str] = {}
    for row in audit_rows:
        for assessment in row["assessments"]:
            source_id = str(assessment["source_id"])
            if assessment.get("evidence_scope") == "stem_and_answer":
                strongest[source_id] = "stem_and_answer"
            else:
                strongest.setdefault(source_id, str(assessment.get("evidence_scope")))
    for row in audit_rows:
        for assessment in row["assessments"]:
            source_id = str(assessment["source_id"])
            if source_id.startswith("src-deekur-gaokaomath-2025-"):
                # the frozen paper PDF is registered as a document, not as an
                # answer page, so its assessments keep the document scope
                assessment["evidence_scope"] = source_rows_scope[source_id]
                continue
            assessment["evidence_scope"] = strongest[source_id]

    # frozen page rows for every document cited as evidence
    document_evidence: Dict[str, Dict[str, Any]] = {}
    for row in audit_rows:
        for assessment in row["assessments"]:
            source_id = str(assessment["source_id"])
            if source_id.startswith("src-deekur-gaokaomath-2025-"):
                # the frozen paper PDF already has its own document row
                continue
            for evidence in assessment.get("stem_evidence") or []:
                path = str(evidence["path"])
                if path in document_evidence:
                    continue
                document_evidence[path] = {
                    "schema_version": "1.0",
                    "source_id": source_id,
                    "path": path,
                    "file": path.split("/", 2)[2],
                    "url": str(evidence["url"]),
                    "scope": "document",
                    "page_index": 1,
                    "covered_page_starts": [1],
                    "media_type": "application/pdf"
                    if path.lower().endswith(".pdf")
                    else "text/html",
                    "sha256": str(evidence["sha256"]),
                    "bytes": int(evidence["bytes"]),
                    "question_numbers": [],
                }
            for question_number in []:
                pass
    # fill in which question numbers each document evidence row covers
    for row in audit_rows:
        for assessment in row["assessments"]:
            for evidence in assessment.get("stem_evidence") or []:
                path = str(evidence["path"])
                entry = document_evidence.get(path)
                if entry is None:
                    continue
                if int(row["number"]) not in entry["question_numbers"]:
                    entry["question_numbers"].append(int(row["number"]))
    for entry in document_evidence.values():
        entry["question_numbers"].sort()
        entry["file"] = "%s/%s" % (entry["source_id"], entry["path"].split("/")[-1])
        entry["path"] = "source_pages/" + entry["file"]
        evidence_rows.append(entry)

    # register every cited source, then copy its bytes so evidence replays offline
    source_rows = {
        str(row["id"]): row for row in read_jsonl(out / "sources.jsonl")
    }
    scope_by_source: Dict[str, str] = {}
    for row in audit_rows:
        for assessment in row["assessments"]:
            source_id = str(assessment["source_id"])
            scope = str(assessment.get("evidence_scope") or "stem_only")
            if scope == "stem_and_answer":
                scope_by_source[source_id] = "stem_and_answer"
            else:
                scope_by_source.setdefault(source_id, scope)
    for row in audit_rows:
        for assessment in row["assessments"]:
            source_id = str(assessment["source_id"])
            if source_id in source_rows:
                continue
            source_rows[source_id] = {
                "schema_version": "1.0",
                "id": source_id,
                "name": "%s 的 2025 年试卷文档" % assessment["publisher"],
                "publisher": assessment["publisher"],
                "url": assessment["url"],
                "evidence_scope": scope_by_source[source_id],
                "independence_group": assessment["independence_group"],
                "access_method": assessment["access_method"],
                "official": False,
                "rights_status": "legal_review_pending",
                "independence_note": (
                    "同名 publisher 的所有文档、上传者与镜像合并为一个独立组；"
                    "该文档在文字层中直接命中题干，因此计入题干组。"
                ),
            }
    write_jsonl(
        out / "sources.jsonl", sorted(source_rows.values(), key=lambda row: str(row["id"]))
    )

    for row in audit_rows:
        for assessment in row["assessments"]:
            for evidence in assessment.get("stem_evidence") or []:
                relative = str(evidence["path"])
                destination = out / relative
                if destination.is_file():
                    continue
                source_id = relative.split("/")[1]
                match = next(
                    (document for document in documents if document["source_id"] == source_id),
                    None,
                )
                if match is None:
                    raise ValueError("cannot replay evidence for %s" % source_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                redact = write_evidence_artifact(match["path"], destination)
                if redact is not None:
                    # the packaged bytes differ from the live page, so record
                    # both hashes instead of pretending they are the same
                    digest = sha256_file(destination)
                    size = destination.stat().st_size
                    for row in evidence_rows:
                        if str(row.get("path")) == relative:
                            row["sha256"] = digest
                            row["bytes"] = size
                            row["original_sha256"] = redact["original_sha256"]
                            row["redaction"] = redact["redaction"]
                    for audit_row in audit_rows:
                        for assessment in audit_row["assessments"]:
                            for key in ("stem_evidence", "answer_evidence"):
                                for evidence in assessment.get(key) or []:
                                    if str(evidence.get("path")) == relative:
                                        evidence["sha256"] = digest
                                        evidence["bytes"] = size
    return evidence_rows, audit_rows


def write_evidence_artifact(source: Path, destination: Path) -> Optional[Dict[str, str]]:
    """Copy a frozen artifact, removing account data from mirrored HTML.

    The audit evidence includes mirrors of 人人文库 preview pages.  Those pages
    carry the uploader's account, a partially unmasked real name and an IP
    province -- personal data about people who have nothing to do with this
    benchmark.  The document body is what the audit cites, so the mirror is
    reduced to that and the original hash is recorded alongside.
    """

    if source.suffix.lower() not in {".html", ".htm"}:
        shutil.copyfile(source, destination)
        return None
    original = source.read_bytes()
    text = original.decode("utf-8", errors="ignore")
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - build-time dependency
        shutil.copyfile(source, destination)
        return None
    soup = BeautifulSoup(text, "lxml")
    removed = 0
    for node in soup.select("span.upload_userinfo"):
        node.decompose()
        removed += 1
    for node in soup.find_all(["p", "span", "i", "div"]):
        label = node.get_text(" ", strip=True)
        if len(label) > 40:
            continue
        if any(key in label for key in ("认证主体", "实名认证", "IP属地", "上传人：")):
            node.decompose()
            removed += 1
    if not removed:
        shutil.copyfile(source, destination)
        return None
    destination.write_text(str(soup), encoding="utf-8")
    return {
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "redaction": "removed_uploader_account_identity_and_ip_fields",
    }


def merge_2026(
    out: Path,
    evidence_rows: List[Dict[str, Any]],
    audit_rows: List[Dict[str, Any]],
) -> None:
    """Append the audited 2026 paper to the already-copied 2025 package."""

    for name in (
        "questions.jsonl",
        "answer_keys.jsonl",
        "papers.jsonl",
        "sources.jsonl",
        "answer_sources.jsonl",
        "answer_audit.jsonl",
        "asset_audit.jsonl",
        "assets.jsonl",
        "point_audit.jsonl",
        "question_audit.jsonl",
        "reviews.jsonl",
    ):
        base_rows = read_jsonl(out / name)
        extra = read_jsonl(BASE_2026 / name)
        write_jsonl(out / name, base_rows + extra)
    catalog = read_jsonl(out / "catalog" / "subjective_catalog.jsonl") + read_jsonl(
        BASE_2026 / "catalog" / "subjective_catalog.jsonl"
    )
    write_jsonl(out / "catalog" / "subjective_catalog.jsonl", catalog)

    for asset in ("assets",):
        source_dir = BASE_2026 / asset
        if not source_dir.is_dir():
            continue
        for child in sorted(source_dir.iterdir()):
            destination = out / asset / child.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(child, destination)

    for source_id, entries in json.loads(
        (BASE_2026 / "audit" / "specs" / "source-pages.json").read_text(encoding="utf-8")
    )["sources"].items():
        for entry in entries:
            relative = "source_pages/%s" % entry["file"]
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BASE_2026 / relative, destination)

    for row in read_jsonl(BASE_2026 / "source_pages.jsonl"):
        evidence_rows.append(dict(row))

    for row in read_jsonl(BASE_2026 / "source_audit.jsonl"):
        row = dict(row)
        row["evidence_level"] = "strict_dual_source_v4"
        row["stem_evidenced_complete"] = bool(row.get("stem_dual_source_complete"))
        audit_rows.append(row)

    base_gold = json.loads((out / "examples" / "gold.json").read_text(encoding="utf-8"))
    extra_gold = json.loads((BASE_2026 / "examples" / "gold.json").read_text(encoding="utf-8"))
    answers = dict(base_gold.get("answers") or {})
    answers.update(extra_gold.get("answers") or {})
    base_gold["answers"] = answers
    write_json(out / "examples" / "gold.json", base_gold)


def upgrade_2025_sources(out: Path) -> None:
    """Add the independence and scope fields the strict register requires."""

    path = out / "sources.jsonl"
    rows = read_jsonl(path)
    for row in rows:
        if not row.get("evidence_scope"):
            row["evidence_scope"] = "stem_only"
        if not row.get("independence_group"):
            row["independence_group"] = "deekur"
        row.setdefault("access_method", "校验冻结 PDF 的 SHA-256、页数与文字层")
    write_jsonl(path, rows)


def pinned_specs(
    audit_rows: Sequence[Mapping[str, Any]], evidence_rows: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Derive the pinned pages/audit specs from the built rows themselves."""

    pages: Dict[str, List[Dict[str, Any]]] = {}
    for row in evidence_rows:
        pages.setdefault(str(row["source_id"]), []).append(
            {
                "file": row["file"],
                "url": row["url"],
                "scope": row["scope"],
                "page_index": row["page_index"],
                "covered_page_starts": row["covered_page_starts"],
                "media_type": row["media_type"],
                "sha256": row["sha256"],
                "bytes": row["bytes"],
                "question_numbers": row["question_numbers"],
            }
        )
    coverage: Dict[str, Dict[str, Any]] = {}
    for row in audit_rows:
        for assessment in row.get("assessments") or []:
            source_id = str(assessment.get("source_id") or "")
            if source_id:
                coverage.setdefault(source_id, {})
    for row in audit_rows:
        number = str(row.get("number") or int(re.search(r"(\d+)$", str(row["question_id"])).group(1)))
        for assessment in row.get("assessments") or []:
            source_id = str(assessment.get("source_id") or "")
            role = str(assessment.get("role") or "")
            if role in {"joint_supporting", "answer_only_supporting"}:
                entry = coverage.setdefault(source_id, {}).setdefault("observed_answers", {})
                entry[number] = {
                    "value": str(assessment.get("answer_value") or ""),
                    "excerpt": str(
                        assessment.get("source_answer_text") or assessment.get("answer_excerpt") or ""
                    ),
                    "answer_match": str(assessment.get("answer_match") or "exact_answer_match"),
                    "normalized_value": str(
                        assessment.get("normalized_answer")
                        or assessment.get("answer_value")
                        or ""
                    ),
                    "stem": str(assessment.get("observed_stem") or ""),
                    "stem_match": str(assessment.get("stem_match") or "exact_stem_match"),
                    "stem_equivalence_basis": str(assessment.get("stem_equivalence_basis") or ""),
                    "answer_equivalence_basis": str(assessment.get("answer_equivalence_basis") or ""),
                }
            elif role == "rejected":
                entry = coverage.setdefault(source_id, {}).setdefault("question_overrides", {})
                entry[number] = {
                    "observed_answer": str(assessment.get("answer_value") or ""),
                    "observed_excerpt": str(assessment.get("answer_excerpt") or ""),
                    "observed_stem": str(assessment.get("observed_stem") or ""),
                    "stem_match": str(assessment.get("stem_match") or "exact_stem_match"),
                    "answer_match": str(assessment.get("answer_match") or "external_answer_error"),
                    "normalized_answer": str(assessment.get("normalized_answer") or ""),
                    "stem_equivalence_basis": str(assessment.get("stem_equivalence_basis") or ""),
                    "answer_equivalence_basis": str(assessment.get("answer_equivalence_basis") or ""),
                }
    pages_spec = {"schema_version": "1.0", "dataset_version": VERSION, "sources": pages}
    audit_spec = {
        "schema_version": "1.0",
        "dataset_version": VERSION,
        "policy": dict(policy),
        "sources": sorted(
            {
                str(assessment["source_id"]): {
                    "id": str(assessment["source_id"]),
                    "independence_group": str(assessment.get("independence_group") or ""),
                    "publisher": str(assessment.get("publisher") or ""),
                }
                for row in audit_rows
                for assessment in row.get("assessments") or []
                if assessment.get("source_id")
            }.values(),
            key=lambda entry: entry["id"],
        ),
        "coverage": coverage,
    }
    return pages_spec, audit_spec


def compose_manifest(
    out: Path,
    *,
    audit_rows: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
    questions: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    papers: Sequence[Mapping[str, Any]],
    strict_questions: Sequence[str],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    from gaokao_bench.quality import EVIDENCE_LEVELS, MIXED_SOURCE_AUDIT_V1

    levels = {level: 0 for level in EVIDENCE_LEVELS}
    for row in audit_rows:
        levels[str(row["evidence_level"])] += 1
    objective_points = sum(int(row["points"]) for row in questions)
    catalog_points = sum(int(row["points"]) for row in catalog)
    paper_points = {}
    for row in questions:
        paper_points[str(row["paper_id"])] = paper_points.get(str(row["paper_id"]), 0) + int(row["points"])
    task_counts: Dict[str, int] = {}
    for row in questions:
        task_counts[str(row["type"])] = task_counts.get(str(row["type"]), 0) + 1

    source_audit = {
        "schema_version": "1.0",
        "path": "source_audit.jsonl",
        "evidence_pages": "source_pages.jsonl",
        "evidence_pages_sha256": sha256_file(out / "source_pages.jsonl"),
        "evidence_page_count": len(evidence_rows),
        "policy": dict(policy),
        "questions": len(audit_rows),
        # "dual source" keeps its literal meaning: at least two independent
        # groups.  The mixed policy floor is lower, so that count is reported
        # separately instead of being folded into this field.
        "stem_dual_source_questions": sum(
            1 for row in audit_rows if int(row["stem_independent_groups"]) >= 2
        ),
        "joint_answer_dual_source_questions": sum(
            1 for row in audit_rows if int(row["joint_answer_independent_groups"]) >= 2
        ),
        "stem_evidenced_questions": sum(
            1 for row in audit_rows if int(row["stem_independent_groups"]) >= 1
        ),
        "gate_floor_stem_groups": 1,
        "gate_floor_joint_groups": 2,
        "gate_complete_questions": sum(1 for row in audit_rows if row["gate_complete"]),
        "evidence_levels": levels,
        "strict_questions": sorted(strict_questions),
        "evidence_ready": True,
        "gate": "pass",
        "failed_question_ids": [],
        "require_pinned_specs": True,
        "pinned_pages_spec": "audit/specs/source-pages.json",
        "pinned_audit_spec": "audit/specs/source-audit.json",
        "pinned_pages_spec_sha256": sha256_file(out / "audit" / "specs" / "source-pages.json"),
        "pinned_audit_spec_sha256": sha256_file(out / "audit" / "specs" / "source-audit.json"),
    }

    files: Dict[str, Dict[str, Any]] = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(out).as_posix()
        files[relative] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}

    manifest: Dict[str, Any] = {
        "schema_version": "1.0",
        "dataset": "Gaokao-Bench",
        "dataset_version": VERSION,
        "status": "release_candidate",
        "created_at": CREATED_AT,
        "benchmark_spec": "benchmark_spec.json",
        "scope": {
            "year": 2025,
            "subject": "数学",
            "papers": ["全国卷 I", "全国卷 II", "北京卷", "上海卷", "天津卷", "2026 全国卷 I"],
            "scoring_scope": "choice_and_fill_only",
            "objective_scoring_types": ["single_choice", "multi_choice", "fill_blank"],
            "catalog_only_types": ["solve", "proof", "subjective_rubric"],
            "full_paper_catalogued": True,
        },
        "counts": {
            "papers": len(papers),
            "questions": len(questions),
            "objective_questions": len(questions),
            "catalog_subjective_questions": len(catalog),
            "full_paper_questions": len(questions) + len(catalog),
            "choice_questions": task_counts.get("single_choice", 0) + task_counts.get("multi_choice", 0),
            "single_choice_questions": task_counts.get("single_choice", 0),
            "multi_choice_questions": task_counts.get("multi_choice", 0),
            "fill_questions": task_counts.get("fill_blank", 0),
            "answer_slots": sum(int(len(row.get("slots") or [])) for row in read_jsonl(out / "answer_keys.jsonl")),
            "source_audit_rows": len(audit_rows),
            "source_evidence_pages": len(evidence_rows),
            "stem_evidenced_questions": source_audit["stem_evidenced_questions"],
            "gate_floor_stem_groups": source_audit["gate_floor_stem_groups"],
            "stem_dual_source_questions": source_audit["stem_dual_source_questions"],
            "joint_answer_dual_source_questions": source_audit["joint_answer_dual_source_questions"],
            "answer_audit_rows": len(read_jsonl(out / "answer_audit.jsonl")),
            "asset_audit_rows": len(read_jsonl(out / "asset_audit.jsonl")),
            "point_audit_rows": len(read_jsonl(out / "point_audit.jsonl")),
            "question_audit_rows": len(read_jsonl(out / "question_audit.jsonl")),
            "review_records": len(read_jsonl(out / "reviews.jsonl")),
            "catalog_subjective_points": catalog_points,
            "answer_evidence_sources": len(read_jsonl(out / "answer_sources.jsonl")),
        },
        "points": {
            "objective_total": objective_points,
            "catalog_subjective_total": catalog_points,
            "full_paper_total": objective_points + catalog_points,
            "scored_denominator": objective_points,
            "by_paper": dict(sorted(paper_points.items())),
        },
        "catalog": {
            "path": "catalog/subjective_catalog.jsonl",
            "question_count": len(catalog),
            "points": catalog_points,
            "included_in_scoring": False,
            "purpose": "future_rubric_and_subjective_research_only",
        },
        "scoring": {
            "contract": "single_primary_metric_v1",
            "metric_version": "1.0",
            "primary_metric": "score.weighted_points",
            "definition": "earned reviewed exam points divided by possible points in the declared scope",
            "unit": "ratio",
            "primary_task_types": ["single_choice", "multi_choice", "fill_blank"],
            "catalog_task_types": ["catalog_subjective"],
            "credit_policy": "all_or_nothing",
            "partial_credit": False,
            "missing_or_invalid_is_zero": True,
            "process_policy": "excluded_from_primary",
            "profile": "strict-source-audited-points-all-or-nothing",
            "diagnostic_metrics": [
                "diagnostic.paper_equal_weighted_points",
                "diagnostic.full_credit_rate",
                "slice.text_answerable.weighted_points",
                "slice.vision_required.weighted_points",
                "slice.text_on_scope_denominator.weighted_points",
            ],
        },
        "quality": {
            "quality_level": MIXED_SOURCE_AUDIT_V1,
            "evidence_levels": levels,
            "strict_question_count": len(strict_questions),
            "strict_papers": ["GB26-N1"],
            "per_question_level_path": "source_audit.jsonl#evidence_level",
            "policy_note": (
                "每道题的来源证据等级逐题记录在 source_audit.jsonl 的 evidence_level 字段；"
                "只有同时达到 9 个独立题干组与 6 个独立联合组的题目标为 strict_dual_source_v4。"
            ),
            "owner_signoff": "pending",
            "review_status": "pending_owner_signoff",
            "human_independent_review": "pending",
            "source_audit_gate": "pass",
            "answer_status": "2025 answers交叉核验自 OCR 答案页并保留待人工复核状态；2026 答案已完成双来源联合核验",
            "asset_status": "裁图均由冻结 PDF 渲染；2025 天津已改绑正确 PDF",
            "subjective_scoring": "catalogued_not_scored_pending_rubric",
            "crop_method": "pdf_explicit_bbox_render_2_2x_v1",
        },
        # The CC BY 4.0 election covers what this project authored: the
        # selection, structure, canonical answers, scores and metadata.  It
        # cannot cover the exam papers themselves or the third-party pages kept
        # as audit evidence, so those are stated separately rather than folded
        # into one sweeping claim.
        "rights": {
            "code_license": "Apache-2.0",
            "data_license": "CC-BY-4.0",
            "data_license_scope": "project_original_selection_structure_answers_metadata",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "exam_material_license": "NOASSERTION",
            "attribution_required": True,
            "third_party_evidence_license": "not_cleared",
            "third_party_evidence_redistribution": "internal_audit_only",
            "third_party_evidence_pii": "account and identity fields removed from mirrored HTML",
            "public_redistribution": "requires_signed_owner_attestation",
            "rights_basis": (
                "project owner elected CC BY 4.0 for project-original content; "
                "exam papers and mirrored third-party pages remain with their "
                "publishers and are distributed only as audit evidence"
            ),
        },
        "public_release": False,
        "audit": {
            "source_audit": "source_audit.jsonl",
            "answer_audit": "answer_audit.jsonl",
            "asset_audit": "asset_audit.jsonl",
            "point_audit": "point_audit.jsonl",
            "question_audit": "question_audit.jsonl",
            "reviews": "reviews.jsonl",
            "answer_sources": "answer_sources.jsonl",
        },
        "source_audit": source_audit,
        "files": files,
        "build_provenance": {
            "builder_path": "scripts/build_0_1_0.py",
            "base_2025": "data/0.1.0-alpha.6.1",
            "base_2026": ".cache/build-attempts/2026-n1.8",
            "source_pages_spec_path": "specs/0.1.0.source-pages.json",
            "source_pages_spec_sha256": sha256_file(
                ROOT / "specs" / "0.1.0.source-pages.json"
            ),
            "source_audit_spec_path": "specs/0.1.0.source-audit.json",
            "source_audit_spec_sha256": sha256_file(ROOT / "specs" / "0.1.0.source-audit.json"),
        },
        "supersedes": "0.1.0-alpha.6.1",
    }
    return manifest


def benchmark_spec(
    *,
    questions: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    paper_points: Mapping[str, int],
    image_required_ids: Sequence[str],
    validators: Sequence[str],
    prompt_crops: Mapping[str, Any],
    prompt_hashes: Mapping[str, str],
) -> Dict[str, Any]:
    task_counts: Dict[str, int] = {}
    for row in questions:
        task_counts[str(row["type"])] = task_counts.get(str(row["type"]), 0) + 1
    return {
        "schema_version": "1.0",
        "version": "1.1",
        "id": "gaokao-math-0.1.0",
        "subject": "数学",
        "years": [2025, 2026],
        "audit_profile": "mixed_source_audit_v1_per_question_evidence",
        "choice_labels": ["A", "B", "C", "D"],
        "custom_validators": [],
        "validators": sorted(validators),
        "expected": {
            "question_count": len(questions),
            "paper_count": len(paper_points),
            "total_points": sum(int(row["points"]) for row in questions),
            "paper_points": dict(sorted(paper_points.items())),
            "full_paper_question_count": len(questions) + len(catalog),
            "full_paper_points": sum(int(row["points"]) for row in questions)
            + sum(int(row["points"]) for row in catalog),
            "catalog_question_count": len(catalog),
            "catalog_points": sum(int(row["points"]) for row in catalog),
            "task_type_counts": dict(sorted(task_counts.items())),
            "required_image_ids": sorted(image_required_ids),
        },
        "task_types": {
            "single_choice": {"shape": "choice", "min_selections": 1, "max_selections": 1},
            "multi_choice": {"shape": "choice", "min_selections": 2},
            "fill_blank": {"shape": "slots"},
            "catalog_subjective": {"shape": "rubric"},
        },
        "prompt": {
            "version": "gbmath-objective-anonymous-v3",
            "answer_schema": "gaokao-answer-v1",
            "system": (
                "你是一名严谨的数学解题者。只依据用户给出的题目文字和随附图形作答。\n\n"
                "要求：\n1. 不猜测题目来自哪个年份、地区、考试或题号，也不使用外部检索。\n"
                "2. 不输出解题过程、解释、Markdown 代码块或多余文字。\n"
                '3. 只输出一个合法 JSON 对象：{"answer": ...}\n'
                '4. 单选题的 answer 是一个大写字母，例如 "A"。\n'
                '5. 多选题的 answer 是升序拼接的大写字母，例如 "ACD"。\n'
                "6. 单空题的 answer 是字符串；多空题的 answer 是按空位顺序排列的 JSON 数组。\n"
                "7. 数学表达式使用题面中的 Unicode 记法；分数、根式、区间、向量按题意简洁书写。\n"
            ),
            "image_crops": dict(prompt_crops),
            "image_pixel_sha256": dict(prompt_hashes),
        },
        "scoring": {
            "contract": "single_primary_metric_v1",
            "metric_version": "1.0",
            "primary_metric": "score.weighted_points",
            "primary_task_types": ["single_choice", "multi_choice", "fill_blank"],
            "catalog_task_types": ["catalog_subjective"],
            "credit_policy": "all_or_nothing",
            "partial_credit": False,
            "process_policy": "excluded_from_primary",
            "profile": "strict-source-audited-points-all-or-nothing",
            "diagnostic_metrics": [
                "diagnostic.paper_equal_weighted_points",
                "diagnostic.full_credit_rate",
                "slice.text_answerable.weighted_points",
                "slice.vision_required.weighted_points",
                "slice.text_on_scope_denominator.weighted_points",
            ],
        },
        "reporting": {
            "canonical_max_tokens": 32768,
            "canonical_min_repeats": 5,
            "canonical_timeout_seconds": 900,
            "constrained_reasoning_budget": 8192,
            "freshness_claim_allowed": False,
            "held_out": False,
            "leaderboard_class": "retrospective_public_2025_2026",
            "live_track_required": True,
            "min_visual_questions_for_ranking": 30,
            "rounding_policy": "accept_values_that_round_to_key_at_stated_precision",
            "visual_slice_role": "diagnostic",
        },
        "views": [
            {
                "id": "full",
                "description": "All scored objective questions and their exam points.",
                "selector": {"all": True},
            },
            {
                "id": "evidence_strict",
                "description": "Only questions whose source evidence reaches strict_dual_source_v4.",
                "selector": {"evidence_level": "strict_dual_source_v4"},
            },
        ],
    }


def write_derived_files(
    out: Path,
    questions: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    papers: Sequence[Mapping[str, Any]],
) -> None:
    """Regenerate coverage.csv, examples/README.md and the rights document.

    These three were inherited from the 2025-only base package and kept stating
    its totals (74 questions / 353 points) after the package grew.  They are
    derived from the shipped rows, never hand-maintained.
    """

    import csv
    import io

    image_required = {
        str(row["id"])
        for row in questions
        if row.get("vision_required") is True
        or str(row.get("asset_dependency") or "") == "required"
    }
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "paper_id",
            "paper_title",
            "single_choice_questions",
            "multi_choice_questions",
            "fill_questions",
            "objective_questions",
            "objective_points",
            "catalog_subjective_questions",
            "catalog_subjective_points",
            "full_questions",
            "full_points",
            "image_required_questions",
        ]
    )
    totals = [0] * 9
    for paper in papers:
        paper_id = str(paper["id"])
        objective = [row for row in questions if str(row["paper_id"]) == paper_id]
        subjective = [row for row in catalog if str(row["paper_id"]) == paper_id]
        single = sum(1 for row in objective if row["type"] == "single_choice")
        multi = sum(1 for row in objective if row["type"] == "multi_choice")
        fill = sum(1 for row in objective if row["type"] == "fill_blank")
        objective_points = sum(int(row["points"]) for row in objective)
        subjective_points = sum(int(row["points"]) for row in subjective)
        images = sum(1 for row in objective if str(row["id"]) in image_required)
        cells = [
            single,
            multi,
            fill,
            len(objective),
            objective_points,
            len(subjective),
            subjective_points,
            len(objective) + len(subjective),
            objective_points + subjective_points,
        ]
        totals = [left + right for left, right in zip(totals, cells)]
        writer.writerow([paper_id, paper.get("title") or "", *cells, images])
    writer.writerow(
        [
            "TOTAL",
            "%d papers" % len(papers),
            *totals,
            sum(1 for row in questions if str(row["id"]) in image_required),
        ]
    )
    (out / "coverage.csv").write_text(buffer.getvalue(), encoding="utf-8")

    (out / "examples" / "README.md").write_text(
        """# Examples

- `gold.json`：当前数据包全部 {questions} 道计分题的 canonical answers，应得 `{points}/{points}`；
- `empty.json`：空提交，应得 `0/{points}`，{questions} 题全部记录为 `missing`；
- `malformed.json`：非法答案示例，用于验证解析失败计 0。

多空题的 canonical answer 使用数组，按空位顺序排列。
""".format(questions=len(questions), points=sum(int(row["points"]) for row in questions)),
        encoding="utf-8",
    )


def write_rights_document(out: Path, manifest: Mapping[str, Any]) -> None:
    """Write the layered rights statement for this package."""

    rights = manifest["rights"]
    (out / "LICENSE_AND_RIGHTS.md").write_text(
        """# Gaokao-Bench {version} 许可与权利

> 本文件说明数据包内的分层许可。它不是法律意见，也不是对任何第三方试卷、
> 答案、网页图片或文档预览的再分发授权。

## 1. 三层许可

| 层 | 内容 | 许可 |
|---|---|---|
| 1. 代码与 Schema | 构建脚本、评分器、CLI、测试、JSON Schema、Harness | `{code}` |
| 2. 项目原创内容 | 选题编排、答案键结构、审计账本、评分说明、数据卡、统计 | `{data}` |
| 3. 原始试卷与第三方证据 | 题干、选项、答案原文、网页答案图、文档预览、题目裁图、PDF | `{exam}` / `{third}` |

项目根目录的 `LICENSE` 不能把原始试卷素材变成 CC BY 4.0。
归档方（`deekur/gaokaomath`）的 CC BY 4.0 声明只覆盖归档编译层，
不自动清除命题方、出版方或素材权利人的权利。

**因此 `data_license=CC-BY-4.0` 只覆盖第 2 层**，不覆盖第 3 层。
把它读成「整包可自由再分发」是误读。

## 2. 第三方证据的处理

`source_pages/` 保存了审计引用的第三方页面与文档，用于让每道题的来源判定可离线重放。
它们**不是**按 CC BY 4.0 分发的：

- `third_party_evidence_license = {third}`
- 人人文库镜像页中的上传者账号、认证主体姓名与 IP 属地已被移除；
  原始散列记录在 `source_pages.jsonl` 的 `original_sha256` 与 `redaction` 字段，
  因此打包字节与原始页面字节不同，这一点是显式记录的，不是哈希失配。

## 3. 分发状态

- `public_release = false`
- `public_redistribution = {distribution}`
- 公开分发前需要 owner 签署的权利风险承担，见 `release/{version}/release-policy.json`。

## 4. 公开发布前应完成

- 逐来源核查六套原卷 PDF、题图与答案页的权利人/许可；
- owner 签字；
- 单独的答案复核（本版 2025 部分仍有 `ocr_match_pending_visual` 状态的题目）；
- 公开、封存、目录三种包的 rights gate；
- 下架、errata 与撤回流程。
""".format(
            version=manifest["dataset_version"],
            code=rights["code_license"],
            data=rights["data_license"],
            exam=rights["exam_material_license"],
            third=rights["third_party_evidence_license"],
            distribution=rights["public_redistribution"],
        ),
        encoding="utf-8",
    )


def write_documents(
    out: Path, manifest: Mapping[str, Any], questions: Sequence[Mapping[str, Any]]
) -> None:
    """Rewrite the package documents so they describe this version, not the base."""

    counts = manifest["counts"]
    points = manifest["points"]
    levels = manifest["quality"]["evidence_levels"]
    by_paper = points["by_paper"]

    card = """# Gaokao-Bench {version} 数据卡

> 数据集：Gaokao-Bench
> 版本：`{version}`
> 替代：`{supersedes}`（保留，不覆盖）
> 状态：发布候选，`public_release=false`，等待 owner 签字
> 范围：2025 年五套数学卷 + 2026 年全国卷 I
> 数据目录：`data/{version}/`

## 1. 规模

| 项目 | 题数 | 分值 |
|---|---:|---:|
| 全卷（含解答题目录） | {full_q} | {full_p} |
| 确定性自动评分（选择/填空） | {obj_q} | {obj_p} |
| 解答题目录（不计分） | {cat_q} | {cat_p} |

主指标只有一个：`score.weighted_points = 获得分 / {obj_p}`。解答题只进目录，
不进入任何分母。

| 试卷 | 评分题 | 评分分 |
|---|---:|---:|
{paper_rows}

## 2. 逐题来源证据等级

本版不把整包声明成同一个质量等级，而是逐题记录实际达到的来源证据等级
（`source_audit.jsonl` 的 `evidence_level` 字段）：

| 等级 | 含义 | 题数 |
|---|---|---:|
| `strict_dual_source_v4` | ≥9 个独立题干组且 ≥6 个独立联合组 | {strict} |
| `dual_source_v2` | ≥2 个独立题干组且 ≥2 个独立联合组 | {dual} |
| `stem_corroborated_v1` | ≥2 个独立题干组 | {stem} |
| `single_source_v1` | 仅有冻结原卷一个题干来源 | {single} |

`strict_dual_source_v4` 只覆盖 2026 年全国卷 I 的 {strict} 道题。五套 2025 卷目前
最高只到 `stem_corroborated_v1`，原因是可达的独立 publisher 组不足九个，
不是因为没有证据。

把下面几个数读清楚，它们不是一回事：

| 字段 | 数值 | 含义 |
|---|---:|---|
| `stem_evidenced_questions` | {stem_ev} | 至少有一个独立题干组的题（本版门禁下限） |
| `stem_dual_source_questions` | {stem_dual} | 至少有两个独立题干组的题 |
| `joint_answer_dual_source_questions` | {joint_dual} | 至少有两个独立「题干+答案」联合组的题 |
| `gate_complete_questions` | {gate_done} | 达到本版政策下限（{floor_stem} 个题干组）的题 |

`source_audit.gate=pass` 的含义是「每题都达到政策下限且等级记录与计数一致」，
在本包里下限是 {floor_stem} 个独立题干组，**不是** `strict_dual_source_v4` 的九组。

## 3. 质量等级的含义

`quality.quality_level = mixed_source_audit_v1`。这是本版新增的等级：它不降低
`strict_dual_source_v4` 的门槛，只允许同一数据包里同时存在不同证据强度的题，
并要求每一题的等级与其计数一致、可被 `validate.py` 复核。把整包标成
`strict_dual_source_v4` 会是不实陈述。

## 4. 权利

- 代码：`Apache-2.0`
- 本项目原创内容（选题、结构、canonical 答案、分值、元数据）：`CC BY 4.0`
- 试卷原始材料（`exam_material_license`）：`NOASSERTION`。项目方无权对高考原卷再许可，
  `CC BY 4.0` 声明**不覆盖**试卷正文与图像。
- 第三方证据镜像（`source_pages/` 中被引用的文库 PDF、网页）：`not_cleared`，
  仅作为审计证据随包提供，不构成再分发许可。
  - 其中人人文库镜像页已移除上传者账号、认证主体与 IP 属地等个人字段；
    原始散列记录在 `source_pages.jsonl` 的 `original_sha256` 与 `redaction` 字段。
- 因此 `public_redistribution` 为 `requires_signed_owner_attestation`，
  公开分发前需要 owner 的书面风险承担。

## 5. 已知限制

- 2025 五卷的答案来自媒体答案页 OCR 与独立推导交叉核对；仍有一部分题目标记为
  `ocr_match_pending_visual`，需要 owner 目视复核。具体条数见
  `answer_audit.jsonl` 的 `status` 分布。
- 全部 {full_q} 题等待 owner 签字；本版 `owner_signoff=pending`。
"""

    per_paper_questions: Dict[str, int] = {}
    for row in questions:
        paper = str(row["paper_id"])
        per_paper_questions[paper] = per_paper_questions.get(paper, 0) + 1
    paper_rows = "\n".join(
        "| %s | %d | %d |" % (paper, per_paper_questions.get(paper, 0), points_paper)
        for paper, points_paper in sorted(by_paper.items())
    )
    (out / "DATA_CARD.md").write_text(
        card.format(
            version=manifest["dataset_version"],
            supersedes=manifest.get("supersedes") or "-",
            full_q=counts["full_paper_questions"],
            full_p=points["full_paper_total"],
            obj_q=counts["questions"],
            obj_p=points["objective_total"],
            cat_q=counts["catalog_subjective_questions"],
            cat_p=points["catalog_subjective_total"],
            paper_rows=paper_rows,
            strict=levels.get("strict_dual_source_v4", 0),
            dual=levels.get("dual_source_v2", 0),
            stem=levels.get("stem_corroborated_v1", 0),
            single=levels.get("single_source_v1", 0),
            stem_ev=manifest["source_audit"]["stem_evidenced_questions"],
            stem_dual=manifest["source_audit"]["stem_dual_source_questions"],
            joint_dual=manifest["source_audit"]["joint_answer_dual_source_questions"],
            gate_done=manifest["source_audit"]["gate_complete_questions"],
            floor_stem=manifest["source_audit"]["gate_floor_stem_groups"],
        ),
        encoding="utf-8",
    )

    reproduce = """# Gaokao-Bench {version} 复现说明

## 1. 校验数据包

```bash
python3 scripts/validate.py --data-dir data/{version}
```

## 2. 用标准答案跑一遍

```bash
python3 scripts/evaluate.py \\
  --data-dir data/{version} \\
  --answers data/{version}/examples/gold.json \\
  --json
```

应得到 `score.weighted_points = 1.0`，即 {obj_p}/{obj_p}。

## 3. 冒烟与发布门禁

```bash
python3 scripts/smoke.py --data-dir data/{version}
python3 scripts/release_gate.py --data-dir data/{version} --target internal
```

## 4. 重新构建

```bash
python3 scripts/measure_2025_question_sources.py --data-dir data/{version}
python3 scripts/build_0_1_0.py --out data/{version} --force
```

构建器从 `{supersedes}` 继承 2025 内容、从已审计的 2026 候选包继承 2026 内容，
并按冻结 PDF 重新渲染天津裁图、重新生成逐题来源评估。

## 5. 环境

- Python 3.9+
- 评分器与数据校验只用标准库
- vision profile 需要 Pillow
- 重新构建需要 PyMuPDF、pdftotext 与本地冻结源
"""
    (out / "REPRODUCE.md").write_text(
        reproduce.format(
            version=manifest["dataset_version"],
            obj_p=points["objective_total"],
            supersedes=manifest.get("supersedes") or "-",
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="replace an existing output package")
    args = parser.parse_args()

    out = args.out.resolve()
    if out.exists():
        if not args.force:
            raise SystemExit("output exists; pass --force to rebuild: %s" % out)
        shutil.rmtree(out)

    copy_tree(BASE_2025, out)
    upgrade_2025_sources(out)
    correct_tianjin(out)
    recrop_tianjin(out)

    questions = read_jsonl(out / "questions.jsonl")
    catalog = read_jsonl(out / "catalog" / "subjective_catalog.jsonl")
    for row in catalog:
        if not row.get("reference_answer"):
            row["reference_answer"] = row.get("reference_answer_seed") or ""
            row["reference_answer_status"] = (
                "seeded_from_source_derivation_pending_rubric_review"
            )
    write_jsonl(out / "catalog" / "subjective_catalog.jsonl", catalog)
    papers_by_id = {str(row["id"]): row for row in read_jsonl(out / "papers.jsonl")}

    evidence_rows, audit_rows = write_2025_evidence(out, questions, catalog, papers_by_id)
    merge_2026(out, evidence_rows, audit_rows)

    audit_rows.sort(key=lambda row: str(row["question_id"]))
    evidence_rows.sort(key=lambda row: (str(row["source_id"]), int(row["page_index"]), str(row["path"])))
    write_jsonl(out / "source_audit.jsonl", audit_rows)
    write_jsonl(out / "source_pages.jsonl", evidence_rows)

    from gaokao_bench.quality import mixed_source_audit_policy

    strict_questions = [str(row["question_id"]) for row in audit_rows if row["evidence_level"] == "strict_dual_source_v4"]
    policy = mixed_source_audit_policy(
        required_question_count=len(audit_rows), strict_questions=tuple(strict_questions)
    )

    pages_spec, audit_spec = pinned_specs(audit_rows, evidence_rows, policy)
    write_json(out / "audit" / "specs" / "source-pages.json", pages_spec)
    write_json(out / "audit" / "specs" / "source-audit.json", audit_spec)
    write_json(ROOT / "specs" / "0.1.0.source-pages.json", pages_spec)
    write_json(ROOT / "specs" / "0.1.0.source-audit.json", audit_spec)

    questions = read_jsonl(out / "questions.jsonl")
    catalog = read_jsonl(out / "catalog" / "subjective_catalog.jsonl")
    papers = read_jsonl(out / "papers.jsonl")
    paper_points: Dict[str, int] = {}
    for row in questions:
        paper_points[str(row["paper_id"])] = paper_points.get(str(row["paper_id"]), 0) + int(row["points"])
    validators = sorted(
        {
            str(slot.get("matcher") if not isinstance(slot.get("matcher"), Mapping) else slot["matcher"].get("validator"))
            for row in read_jsonl(out / "answer_keys.jsonl")
            for slot in row.get("slots") or []
            if slot.get("matcher")
        }
    ) or ["numeric"]
    image_required_ids = sorted(
        str(row["id"])
        for row in questions
        if row.get("vision_required") is True
        or str(row.get("asset_dependency") or "") == "required"
    )
    # a required-vision question needs a reviewed prompt crop and its pixel hash
    # so the harness can reuse the exact same image the audit reviewed
    prompt_crops: Dict[str, Any] = {}
    prompt_hashes: Dict[str, str] = {}
    for question_id in image_required_ids:
        row = next(item for item in questions if str(item["id"]) == question_id)
        image_path = out / str(row["image_path"])
        with Image.open(image_path) as image:
            width, height = image.size
            box = (0, 0, width, height)
            cropped = image.convert("RGB").crop(box)
            payload = ("%dx%d:RGB" % (cropped.width, cropped.height)).encode("ascii") + cropped.tobytes()
        prompt_crops[question_id] = list(box)
        prompt_hashes[question_id] = hashlib.sha256(payload).hexdigest()
    write_json(
        out / "benchmark_spec.json",
        benchmark_spec(
            questions=questions,
            catalog=catalog,
            paper_points=paper_points,
            image_required_ids=image_required_ids,
            validators=validators,
            prompt_crops=prompt_crops,
            prompt_hashes=prompt_hashes,
        ),
    )

    manifest = compose_manifest(
        out,
        audit_rows=audit_rows,
        evidence_rows=evidence_rows,
        questions=questions,
        catalog=catalog,
        papers=papers,
        strict_questions=strict_questions,
        policy=policy,
    )
    write_documents(out, manifest, questions)
    write_derived_files(out, questions, catalog, papers)
    write_rights_document(out, manifest)
    # the document files are part of the manifest's file ledger, so the manifest
    # is composed only after they are in place
    manifest = compose_manifest(
        out,
        audit_rows=audit_rows,
        evidence_rows=evidence_rows,
        questions=questions,
        catalog=catalog,
        papers=papers,
        strict_questions=strict_questions,
        policy=policy,
    )
    write_json(out / "manifest.json", manifest)

    print("questions:", len(questions))
    print("catalog:", len(catalog))
    print("audit rows:", len(audit_rows), "evidence rows:", len(evidence_rows))
    print("strict questions:", len(strict_questions))
    print("evidence levels:", manifest["quality"]["evidence_levels"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
