#!/usr/bin/env python3
"""Freeze every page-level evidence asset cited by the 2026 N1 source audit.

The generated JSON file is intentionally small: it contains URLs, cache-relative
file names, SHA-256 digests, byte counts and the logical page ranges covered by
each file.  The bytes live outside git in a local cache; the data builder copies
them into the immutable package, so acceptance reviews can replay the evidence
offline without trusting a live URL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET_SPEC = ROOT / "specs" / "2026-math-n1.json"
DEFAULT_AUDIT_SPEC = ROOT / "specs" / "2026-math-n1.source-audit.json"
DEFAULT_OUTPUT = ROOT / "specs" / "2026-math-n1.source-pages.json"
DEFAULT_CACHE = ROOT / ".cache" / "gaokao-source-pages" / "2026-n1"


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object: %s" % path)
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _media_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data.startswith(b"%PDF-"):
        return "application/pdf", ".pdf"
    prefix = data[:512].lstrip().lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        return "text/html", ".html"
    raise ValueError("unrecognised evidence payload; refusing to freeze it")


def _group_pages(groups: Iterable[Mapping[str, Any]]) -> List[int]:
    pages: set[int] = set()
    for group in groups:
        start = group.get("page_start")
        if start is None:
            continue
        end = group.get("page_end", start)
        try:
            first = int(start)
            last = int(end)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid group page range: %r" % group) from exc
        if first < 1 or last < first:
            raise ValueError("invalid group page range: %r" % group)
        pages.update(range(first, last + 1))
    return sorted(pages)


def _download(url: str, *, referer: str) -> bytes:
    response = requests.get(
        url,
        timeout=90,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
            **({"Referer": referer} if referer else {}),
        },
    )
    response.raise_for_status()
    if len(response.content) < 64:
        raise ValueError("evidence response is implausibly small: %s" % url)
    return response.content


def _write_entry(
    *,
    cache_dir: Path,
    source_id: str,
    filename: str,
    url: str,
    referer: str,
    data: bytes | None,
    scope: str,
    page_index: int,
    covered_pages: Sequence[int],
) -> Dict[str, Any]:
    payload = data if data is not None else _download(url, referer=referer)
    media_type, extension = _media_type(payload)
    if not filename.endswith(extension):
        filename = str(Path(filename).with_suffix(extension))
    relative = "%s/%s" % (source_id, filename)
    destination = cache_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return {
        "file": relative,
        "url": url,
        "scope": scope,
        "page_index": int(page_index),
        "covered_page_starts": sorted({int(value) for value in covered_pages}),
        "media_type": media_type,
        "sha256": _sha256(payload),
        "bytes": len(payload),
    }


def _page_urls_for_source(
    source: Mapping[str, Any], coverage: Mapping[str, Any]
) -> List[tuple[int, str]]:
    urls = source.get("page_urls")
    if not isinstance(urls, Mapping):
        return []
    groups = (
        list(coverage.get("stem_groups") or [])
        + list(coverage.get("joint_answer_groups") or [])
        + list(coverage.get("answer_groups") or [])
    )
    return [
        (page, str(urls.get(str(page)) or ""))
        for page in _group_pages(groups)
        if str(urls.get(str(page)) or "").strip()
    ]


def _question_numbers_by_page(
    coverage: Mapping[str, Any], *, scope: str
) -> Dict[int, set[int]]:
    if scope == "stem":
        groups = list(coverage.get("stem_groups") or [])
    elif scope in {"answer", "stem_and_answer"}:
        groups = list(coverage.get("joint_answer_groups") or []) + list(
            coverage.get("answer_groups") or []
        )
    else:
        groups = (
            list(coverage.get("stem_groups") or [])
            + list(coverage.get("joint_answer_groups") or [])
            + list(coverage.get("answer_groups") or [])
        )
    by_page: Dict[int, set[int]] = {}
    for group in groups:
        if not isinstance(group, Mapping):
            continue
        first = int(group.get("page_start") or 1)
        last = int(group.get("page_end", first))
        numbers = {int(number) for number in group.get("numbers", [])}
        for page in range(first, last + 1):
            by_page.setdefault(page, set()).update(numbers)
    return by_page


def _attach_question_numbers(
    entries: Sequence[Dict[str, Any]],
    *,
    coverage: Mapping[str, Any],
    source_id: str,
    fallback_numbers: Sequence[int],
) -> None:
    stem_by_page = _question_numbers_by_page(coverage, scope="stem")
    answer_by_page = _question_numbers_by_page(coverage, scope="answer")
    page_sequence_by_page = _question_numbers_by_page(coverage, scope="all")
    all_numbers = sorted(
        {
            number
            for values in page_sequence_by_page.values()
            for number in values
        }
        or {int(number) for number in fallback_numbers}
    )
    page_overrides = coverage.get("page_question_numbers")
    for entry in entries:
        if entry.get("scope") == "document":
            numbers = all_numbers
        else:
            scope = str(entry.get("scope"))
            if scope == "stem":
                by_page = stem_by_page
            elif scope == "answer":
                by_page = answer_by_page
            else:
                by_page = page_sequence_by_page
            numbers = sorted(
                {
                    number
                    for page in entry.get("covered_page_starts", [])
                    for number in by_page.get(int(page), set())
                }
            )
            if isinstance(page_overrides, Mapping):
                overridden: set[int] = set()
                for page in entry.get("covered_page_starts", []):
                    values = page_overrides.get(str(int(page)))
                    if isinstance(values, list):
                        overridden.update(int(number) for number in values)
                if overridden:
                    numbers = sorted(overridden)
        if not numbers:
            raise ValueError(
                "source page evidence is not mapped to any question number: %s/%s"
                % (source_id, entry.get("file"))
            )
        entry["question_numbers"] = numbers


def build_source_page_spec(
    *,
    dataset_spec: Mapping[str, Any],
    audit_spec: Mapping[str, Any],
    cache_dir: Path,
    pdf_path: Path,
) -> Dict[str, Any]:
    source_rows: Dict[str, List[Dict[str, Any]]] = {}
    primary = dataset_spec["source"]
    primary_id = str(primary["id"])
    all_question_numbers = sorted(
        {
            int(number)
            for source_coverage in audit_spec["coverage"].values()
            if isinstance(source_coverage, Mapping)
            for key in ("stem_groups", "joint_answer_groups", "answer_groups")
            for group in source_coverage.get(key, [])
            if isinstance(group, Mapping)
            for number in group.get("numbers", [])
        }
    )

    for source in audit_spec["sources"]:
        source_id = str(source["id"])
        coverage = audit_spec["coverage"].get(source_id, {})
        entries: List[Dict[str, Any]] = []

        if source_id == primary_id:
            payload = pdf_path.read_bytes()
            entries.append(
                _write_entry(
                    cache_dir=cache_dir,
                    source_id=source_id,
                    filename="source.pdf",
                    url=str(primary["url"]),
                    referer="",
                    data=payload,
                    scope="document",
                    page_index=1,
                    covered_pages=range(1, int(primary["page_count"]) + 1),
                )
            )

        page_urls = _page_urls_for_source(source, coverage)
        for page, url in page_urls:
            entries.append(
                _write_entry(
                    cache_dir=cache_dir,
                    source_id=source_id,
                    filename="page-%02d.img" % page,
                    url=url,
                    referer=str(source.get("url") or ""),
                    data=None,
                    scope="page",
                    page_index=page,
                    covered_pages=[page],
                )
            )

        stem_urls = [str(value) for value in source.get("stem_image_urls") or []]
        if stem_urls:
            pages = _group_pages(coverage.get("stem_groups") or [])
            for index, url in enumerate(stem_urls, 1):
                covered = pages if len(stem_urls) == 1 else [page for page in pages if page == index]
                if not covered:
                    raise ValueError(
                        "stem image %d of %s is not mapped to any logical source page"
                        % (index, source_id)
                    )
                entries.append(
                    _write_entry(
                        cache_dir=cache_dir,
                        source_id=source_id,
                        filename="stem-%02d.img" % index,
                        url=url,
                        referer=str(source.get("url") or ""),
                        data=None,
                        scope="stem",
                        page_index=index,
                        covered_pages=covered,
                    )
                )

        answer_urls = [str(value) for value in source.get("answer_image_urls") or []]
        if answer_urls:
            groups = list(coverage.get("joint_answer_groups") or []) + list(
                coverage.get("answer_groups") or []
            )
            pages = _group_pages(groups) or [1]
            stem_pages = set(_group_pages(coverage.get("stem_groups") or []))
            for index, url in enumerate(answer_urls, 1):
                covered = pages if len(answer_urls) == 1 else [page for page in pages if page == index]
                if not covered:
                    raise ValueError(
                        "answer image %d of %s is not mapped to any logical source page"
                        % (index, source_id)
                    )
                entries.append(
                    _write_entry(
                        cache_dir=cache_dir,
                        source_id=source_id,
                        filename="answer-%02d.img" % index,
                        url=url,
                        referer=str(source.get("url") or ""),
                        data=None,
                        scope=(
                            "stem_and_answer"
                            if source.get("answer_pages_include_stems") is True
                            and index in stem_pages
                            else "answer"
                        ),
                        page_index=index,
                        covered_pages=covered,
                    )
                )

        if not entries and source.get("evidence_scope") == "answer_only":
            entries.append(
                _write_entry(
                    cache_dir=cache_dir,
                    source_id=source_id,
                    filename="page.html",
                    url=str(source["url"]),
                    referer="",
                    data=None,
                    scope="answer",
                    page_index=1,
                    covered_pages=[1],
                )
            )

        if not entries:
            raise ValueError("source has no freezeable page evidence: %s" % source_id)
        _attach_question_numbers(
            entries,
            coverage=coverage,
            source_id=source_id,
            fallback_numbers=all_question_numbers,
        )
        source_rows[source_id] = entries

    return {
        "schema_version": "1.0",
        "dataset_version": dataset_spec["dataset_version"],
        "sources": source_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-spec", type=Path, default=DEFAULT_DATASET_SPEC)
    parser.add_argument("--audit-spec", type=Path, default=DEFAULT_AUDIT_SPEC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--pdf",
        type=Path,
        default=ROOT / ".cache" / "gaokao-source" / "2026-national-1-math.pdf",
    )
    parser.add_argument("--clean", action="store_true", help="clear the evidence cache first")
    args = parser.parse_args()

    dataset_spec = _read_json(args.dataset_spec.resolve())
    audit_spec = _read_json(args.audit_spec.resolve())
    pdf_path = args.pdf.resolve()
    cache_dir = args.cache_dir.resolve()
    if args.clean and cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output = build_source_page_spec(
        dataset_spec=dataset_spec,
        audit_spec=audit_spec,
        cache_dir=cache_dir,
        pdf_path=pdf_path,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    page_count = sum(len(rows) for rows in output["sources"].values())
    byte_count = sum(int(row["bytes"]) for rows in output["sources"].values() for row in rows)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "cache_dir": str(cache_dir),
                "sources": len(output["sources"]),
                "evidence_files": page_count,
                "bytes": byte_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
