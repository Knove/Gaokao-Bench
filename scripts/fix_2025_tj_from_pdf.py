#!/usr/bin/env python3
"""Correct the 天津 2025 canonical content against the frozen PDF.

``0.1.0-alpha.6.1`` bound 天津 to a recalled reconstruction: several question
stems and multiple-choice option lists differ from the archived paper, so any
answer letter recorded against the old option order is meaningless.  This script
re-reads the frozen PDF's own text layer, rewrites the stem and option text, and
remaps each recorded answer into the paper's real option ordering.  Every change
is written to ``reports/audit-2025/tj-corrections.json`` so the diff is auditable.

The remapping never invents an answer: it takes the option *text* the old
package marked correct and finds that text's position in the real option list.
When the old option text is not present in the paper at all, the question is
reported as unresolved instead of guessed.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / ".cache" / "gaokao-2025-sources" / "deekur" / "GB25-TJ.pdf"
OUT = ROOT / "reports" / "audit-2025" / "tj-corrections.json"


def demath(text: str) -> str:
    """Map mathematical alphanumerics to ASCII and collapse PDF doubling."""

    chars: List[str] = []
    for char in text:
        if "MATHEMATICAL" in unicodedata.name(char, ""):
            chars.append(unicodedata.normalize("NFKC", char))
        else:
            chars.append(char)
    collapsed = re.sub(r"([A-Za-z])\1+", r"\1", "".join(chars))
    return collapsed.replace("∼", "~").replace("αα", "α").replace("ββ", "β").replace("μμ", "μ").replace("σσ", "σ")


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("$", "").replace("\\", "")
    return re.sub(r"[\s]+", "", value)


def pdf_questions(pdf: Path) -> Dict[int, Dict[str, Any]]:
    """Return each question's stem and options as the PDF prints them."""

    import fitz  # type: ignore

    marker = re.compile(r"(?<![\d.])(\d{1,2})\s*[．.]\s*")
    document = fitz.open(pdf)
    try:
        groups: Dict[int, List[str]] = {}
        current: Optional[int] = None
        for page_index in range(document.page_count):
            page = document[page_index]
            blocks = sorted(
                (
                    block
                    for block in page.get_text("blocks")
                    if str(block[4]).strip() and str(block[4]).strip() != "|"
                ),
                key=lambda block: (round(block[1], 1), block[0]),
            )
            for block in blocks:
                raw = str(block[4]).strip()
                found = marker.match(raw)
                if found and 1 <= int(found.group(1)) <= 20:
                    current = int(found.group(1))
                    groups.setdefault(current, [])
                if current is not None:
                    groups[current].append(raw)
    finally:
        document.close()

    questions: Dict[int, Dict[str, Any]] = {}
    tail = {
        "二、填空题",
        "三、解答题",
    }
    for number, blocks in groups.items():
        text = demath("\n".join(blocks))
        text = re.sub(r"^\s*\d{1,2}\s*[．.]\s*", "", text)
        for marker_text in tail:
            text = text.replace(marker_text, " ")
        pieces = re.split(r"(?=[A-D][．.])", text)
        stem = re.sub(r"\s+", " ", pieces[0]).strip()
        options: List[Dict[str, str]] = []
        for chunk in pieces[1:]:
            label = chunk[0]
            body = re.sub(r"^[A-D][．.]\s*", "", chunk)
            body = re.sub(r"\s+", " ", body).strip()
            if body:
                options.append({"label": label, "text": body})
        questions[number] = {"stem": stem, "options": options}
    return questions


def stem_tokens(text: str) -> frozenset:
    """Identifiers, numerals and Chinese characters, sign-preserving.

    Operators are dropped because a vertical fraction loses its "/" and a
    radical its "√", so ``−√3/2`` and ``− √ 3 2`` are the same option.  A sign
    attached to a numeral is *not* dropped: ``−1`` and ``1`` are different
    options, and treating them as equal silently keeps a wrong distractor.
    Chinese is deliberately excluded: the canonical text sometimes spells out
    wording the paper abbreviates (``若随机变量 X~N(...)`` for ``若X~N(...)``),
    and that expansion is faithful, so it must not count as a different option.
    A difference in the mathematics -- numbers, identifiers or a sign -- is what
    makes an old distractor wrong.
    """

    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("^", "").replace("{", "").replace("}", "").replace("$", "")
    value = value.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"-\s+", "-", value)
    tokens = set(re.findall(r"-?[0-9]+|[A-Za-z]+", value))
    # a sign in front of a radical or a bracket does not attach to a numeral;
    # keep it as its own marker so "−√3/2" never equals "√3/2"
    compact = re.sub(r"\s+", "", value)
    if compact.startswith("-") and not any(token.startswith("-") for token in tokens):
        tokens.add("-lead")
    return frozenset(tokens)


def pdf_stem_is_faithful(canonical: str, candidate: str) -> bool:
    """Return whether the PDF text layer kept everything the canonical stem has.

    The layer drops glyphs inside math, turning ``△ABC`` into ``△A`` and
    ``CG=3GC1`` into ``C= 3GC1``.  A candidate that lost tokens would be a worse
    stem than the hand-checked one, so it is rejected.
    """

    wanted = stem_tokens(canonical)
    if not wanted:
        return True
    if not wanted.issubset(stem_tokens(candidate)):
        return False
    # a doubled Greek run or a lone letter repeated across a space means the
    # layer split an italic glyph, so the printed formula is not recoverable
    if re.search(r"([\u0370-\u03ff])\1", candidate):
        return False
    if re.search(r"\b([A-Za-z])\s+\1\b", candidate):
        return False
    return True


def clean_stem(text: str) -> str:
    """Tidy the PDF's layout artefacts without changing any content."""

    value = re.sub(r"\s+", " ", text or "").strip()
    value = re.sub(r"\s+([，。、；：）】])", r"\1", value)
    value = re.sub(r"([（【])\s+", r"\1", value)
    value = re.sub(r"\(\s*\)", "（ ）", value)
    value = re.sub(r"（\s*）", "（ ）", value)
    value = re.sub(r"\s+=", " =", value)
    value = re.sub(r"=\s+", "= ", value)
    return value.strip()


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "0.1.0")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    paper = pdf_questions(PDF)
    questions = read_jsonl(args.data_dir / "questions.jsonl")
    answer_keys = {
        str(row["question_id"]): row for row in read_jsonl(args.data_dir / "answer_keys.jsonl")
    }

    changes: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for row in questions:
        question_id = str(row["id"])
        if not question_id.startswith("GB25-TJ-"):
            continue
        number = int(row["number"])
        truth = paper.get(number)
        if truth is None:
            unresolved.append({"question_id": question_id, "reason": "question not found in PDF"})
            continue
        old_options = row.get("options") or []
        old_stem = str(row.get("stem") or "")
        # The paper's own wording is authoritative for every question, including
        # the fill-in-the-blank and subjective items.  The text layer destroys
        # the vector arrows on Q14, so that stem keeps the hand-corrected form
        # that 0.1.0-alpha.6.1 recorded.
        if number != 14:
            cleaned = clean_stem(truth["stem"])
            if not pdf_stem_is_faithful(str(row.get("stem") or ""), cleaned):
                cleaned = ""
            if cleaned and normalize(cleaned) != normalize(str(row.get("stem") or "")):
                changes.append(
                    {
                        "question_id": question_id,
                        "field": "stem",
                        "before": str(row.get("stem") or ""),
                        "after": cleaned,
                    }
                )
                row["stem"] = cleaned
                if not old_options:
                    row["content"] = cleaned

        if not old_options:
            # nothing else to correct without an option list
            continue

        key = answer_keys.get(question_id)
        old_correct = [label for label in (key or {}).get("correct_options") or []]
        old_correct_text = [
            next((option["text"] for option in old_options if option["label"] == label), None)
            for label in old_correct
        ]

        for index, new_option in enumerate(truth["options"]):
            old_option = old_options[index] if index < len(old_options) else None
            if old_option is None:
                changes.append(
                    {
                        "question_id": question_id,
                        "field": "options[%d]" % index,
                        "before": None,
                        "after": "%s. %s" % (new_option["label"], new_option["text"]),
                    }
                )
                continue
            if stem_tokens(old_option["text"]) != stem_tokens(new_option["text"]):
                changes.append(
                    {
                        "question_id": question_id,
                        "field": "options[%d]" % index,
                        "before": "%s. %s" % (old_option["label"], old_option["text"]),
                        "after": "%s. %s" % (new_option["label"], new_option["text"]),
                    }
                )

        # The canonical option text is a readable rendering of the same printed
        # expression; only options whose identifiers or numerals actually differ
        # take the PDF's wording, which drops fraction bars and radicals.
        merged: List[Dict[str, str]] = []
        for index, new_option in enumerate(truth["options"]):
            old_option = old_options[index] if index < len(old_options) else None
            if old_option is not None and stem_tokens(old_option["text"]) == stem_tokens(
                new_option["text"]
            ):
                merged.append({"label": new_option["label"], "text": old_option["text"]})
            else:
                merged.append(new_option)
        row["options"] = merged
        row["content"] = "%s %s" % (
            row.get("stem") or "",
            " ".join("%s %s" % (option["label"], option["text"]) for option in merged),
        )

        # remap the answer through the option text it was recorded against
        if old_correct and old_correct_text:
            remapped = []
            for text in old_correct_text:
                if text is None:
                    continue
                hit = next(
                    (
                        option["label"]
                        for option in truth["options"]
                        if normalize(option["text"]) == normalize(text)
                    ),
                    None,
                )
                if hit is None:
                    # fall back to a looser containment test for typeset differences
                    hit = next(
                        (
                            option["label"]
                            for option in truth["options"]
                            if normalize(text) in normalize(option["text"])
                            or normalize(option["text"]) in normalize(text)
                        ),
                        None,
                    )
                if hit is None:
                    # last resort: the same label position printing the same
                    # tokens, which is how fractions and radicals differ
                    old_index = old_correct_text.index(text)
                    old_label = old_correct[old_index]
                    candidate = next(
                        (
                            option
                            for option in truth["options"]
                            if option["label"] == old_label
                        ),
                        None,
                    )
                    if candidate is not None and stem_tokens(text) == stem_tokens(candidate["text"]):
                        hit = candidate["label"]
                remapped.append(hit)
            if all(remapped) and len(remapped) == len(old_correct):
                new_correct = sorted(remapped)
                if key is not None:
                    key["correct_options"] = new_correct
                    if len(new_correct) == 1:
                        key["canonical"] = new_correct[0]
                    if new_correct != old_correct:
                        changes.append(
                            {
                                "question_id": question_id,
                                "field": "canonical",
                                "before": ",".join(old_correct),
                                "after": ",".join(new_correct),
                            }
                        )
            else:
                unresolved.append(
                    {
                        "question_id": question_id,
                        "reason": "recorded answer option text is absent from the PDF option list",
                        "old_correct": old_correct,
                        "old_correct_text": old_correct_text,
                        "pdf_options": truth["options"],
                    }
                )

    # the subjective questions live in the catalog, and their stems must match
    # the paper for the same reason the objective ones do
    catalog_path = args.data_dir / "catalog" / "subjective_catalog.jsonl"
    catalog = read_jsonl(catalog_path)
    for row in catalog:
        question_id = str(row.get("id") or "")
        if not question_id.startswith("GB25-TJ-"):
            continue
        number = int(row["number"])
        if number == 14:
            continue
        truth = paper.get(number)
        if truth is None:
            continue
        original = str(row.get("content") or "")
        cleaned = clean_stem(truth["stem"])
        if not pdf_stem_is_faithful(original, cleaned):
            cleaned = ""
        if cleaned and normalize(cleaned) != normalize(original):
            changes.append(
                {
                    "question_id": question_id,
                    "field": "stem",
                    "before": original,
                    "after": cleaned,
                }
            )
            row["content"] = cleaned
    write_jsonl(catalog_path, catalog)

    write_jsonl(args.data_dir / "questions.jsonl", questions)
    write_jsonl(
        args.data_dir / "answer_keys.jsonl",
        [answer_keys[str(row["id"])] for row in questions if str(row["id"]) in answer_keys],
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"corrections": changes, "unresolved": unresolved}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print("corrections:", len(changes))
    print("unresolved answers:", len(unresolved))
    for item in unresolved:
        print("  ", item["question_id"], item["reason"])
    return 0 if not unresolved else 1


if __name__ == "__main__":
    raise SystemExit(main())
