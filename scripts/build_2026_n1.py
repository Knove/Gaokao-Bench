#!/usr/bin/env python3
"""Build the reproducible 2026 National I mathematics acceptance candidate.

The builder starts from the frozen source PDF, re-crops every question, and
writes the scored objective package plus the full-paper subjective catalogue.
It never calls a model provider and it refuses to build unless the source PDF
hash matches the recorded exam paper.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.benchmark_spec import parse_benchmark_spec  # noqa: E402
from gaokao_bench.quality import QUALITY_LEVEL  # noqa: E402
from gaokao_bench.scoring import public_scoring_contract  # noqa: E402
from gaokao_bench.version import ENGINE_VERSION  # noqa: E402


SPEC_PATH = ROOT / "specs" / "2026-math-n1.json"
SOURCE_AUDIT_SCHEMA_VERSION = "1.0"
CROP_METHOD = "pdf_explicit_bbox_render_2_2x_v1"
SCHEMA_VERSION = "1.0"
SOURCE_REPOSITORY = "https://github.com/Knove/Gaokao-Bench.git"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object: %s" % path)
    return value


def source_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def worktree_is_dirty() -> bool:
    try:
        status = subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("cannot verify git worktree cleanliness") from exc
    return bool(status.strip())


def load_source_audit_spec(spec: Mapping[str, Any]) -> Dict[str, Any]:
    relative = str(spec.get("source_audit_spec") or "").strip()
    if not relative:
        raise ValueError("spec is missing source_audit_spec")
    path = (ROOT / relative).resolve()
    if ROOT not in path.parents or not path.is_file():
        raise ValueError("source audit spec does not exist: %s" % path)
    value = read_json(path)
    if not isinstance(value.get("sources"), list) or not isinstance(value.get("coverage"), dict):
        raise ValueError("source audit spec must contain sources and coverage")
    policy = value.get("policy")
    if not isinstance(policy, dict):
        raise ValueError("source audit spec must contain a policy object")
    expected = spec.get("source_audit_policy")
    if isinstance(expected, dict):
        for key, expected_value in expected.items():
            if policy.get(key) != expected_value:
                raise ValueError(
                    "source audit policy mismatch for %s: %s != %s"
                    % (key, policy.get(key), expected_value)
                )
    return value


def load_source_pages_spec(
    spec: Mapping[str, Any], audit_spec: Mapping[str, Any]
) -> Dict[str, Any]:
    relative = str(spec.get("source_pages_spec") or "").strip()
    if not relative:
        raise ValueError("spec is missing source_pages_spec")
    path = (ROOT / relative).resolve()
    if ROOT not in path.parents or not path.is_file():
        raise ValueError("source page evidence spec does not exist: %s" % path)
    value = read_json(path)
    if value.get("dataset_version") != spec.get("dataset_version"):
        raise ValueError("source page evidence spec version does not match dataset version")
    sources = value.get("sources")
    if not isinstance(sources, Mapping):
        raise ValueError("source page evidence spec must contain a sources object")
    expected = {
        str(source["id"])
        for source in audit_spec.get("sources", [])
        if isinstance(source, Mapping) and source.get("id")
    }
    if set(sources) != expected:
        missing = sorted(expected - set(sources))
        extra = sorted(set(sources) - expected)
        raise ValueError(
            "source page evidence coverage mismatch; missing=%s extra=%s" % (missing, extra)
        )
    for source_id, rows in sources.items():
        if not isinstance(rows, list) or not rows:
            raise ValueError("source page evidence is empty for %s" % source_id)
        seen_files: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("source page evidence row must be an object for %s" % source_id)
            relative_file = str(row.get("file") or "").strip()
            if not relative_file or relative_file in seen_files:
                raise ValueError("source page evidence file is missing or duplicated: %s" % source_id)
            seen_files.add(relative_file)
            if not str(row.get("url") or "").startswith(("http://", "https://")):
                raise ValueError("source page evidence URL is invalid for %s" % source_id)
            if row.get("scope") not in {"stem", "answer", "stem_and_answer", "page", "document"}:
                raise ValueError("source page evidence scope is invalid for %s" % source_id)
            digest = str(row.get("sha256") or "")
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("source page evidence SHA-256 is invalid for %s" % source_id)
            if int(row.get("bytes") or 0) <= 0:
                raise ValueError("source page evidence byte count is invalid for %s" % source_id)
            pages = row.get("covered_page_starts")
            if not isinstance(pages, list) or not pages or any(int(page) < 1 for page in pages):
                raise ValueError("source page evidence page coverage is invalid for %s" % source_id)
            numbers = row.get("question_numbers")
            if (
                not isinstance(numbers, list)
                or not numbers
                or any(int(number) < 1 for number in numbers)
            ):
                raise ValueError("source page evidence question mapping is invalid for %s" % source_id)
    return value


def materialize_source_pages(
    *,
    source_pages_spec: Mapping[str, Any],
    cache_dir: Path,
    output_dir: Path,
) -> tuple[List[Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    """Copy and hash-check the frozen page evidence used by the source audit."""

    rows: List[Dict[str, Any]] = []
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    seen_paths: set[str] = set()
    for source_id, entries in source_pages_spec.get("sources", {}).items():
        if not isinstance(entries, list) or not entries:
            raise ValueError("source page evidence is empty for %s" % source_id)
        source_rows: List[Dict[str, Any]] = []
        for entry in entries:
            relative_file = str(entry["file"])
            relative_path = Path("source_pages") / relative_file
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError("unsafe source page evidence path: %s" % relative_file)
            if relative_path.as_posix() in seen_paths:
                raise ValueError("duplicate source page evidence path: %s" % relative_path)
            seen_paths.add(relative_path.as_posix())
            cache_path = (cache_dir / relative_file).resolve()
            if not cache_path.is_file():
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                request = urllib.request.Request(
                    str(entry["url"]), headers={"User-Agent": "Gaokao-Bench/0.1"}
                )
                with urllib.request.urlopen(request, timeout=90) as response:
                    cache_path.write_bytes(response.read())
            payload = cache_path.read_bytes()
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            if actual_sha256 != entry["sha256"]:
                raise ValueError(
                    "source page evidence hash mismatch for %s: %s != %s"
                    % (relative_file, actual_sha256, entry["sha256"])
                )
            if len(payload) != int(entry["bytes"]):
                raise ValueError("source page evidence byte count mismatch for %s" % relative_file)
            destination = output_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cache_path, destination)
            if sha256(destination) != entry["sha256"]:
                raise ValueError("copied source page evidence does not retain its hash: %s" % relative_file)
            row = {
                "schema_version": "1.0",
                "source_id": str(source_id),
                "path": relative_path.as_posix(),
                "file": relative_file,
                "url": str(entry["url"]),
                "scope": str(entry["scope"]),
                "page_index": int(entry["page_index"]),
                "covered_page_starts": sorted(int(page) for page in entry["covered_page_starts"]),
                "media_type": str(entry["media_type"]),
                "sha256": actual_sha256,
                "bytes": len(payload),
                "question_numbers": sorted(
                    int(number) for number in entry.get("question_numbers", [])
                ),
            }
            rows.append(row)
            source_rows.append(row)
        by_source[str(source_id)] = source_rows
    rows.sort(key=lambda row: (row["source_id"], row["page_index"], row["path"]))
    return rows, by_source


def _group_for_number(
    groups: Any, number: int, *, source_id: str, kind: str
) -> Optional[Mapping[str, Any]]:
    if not isinstance(groups, list):
        return None
    matches = [
        group
        for group in groups
        if isinstance(group, Mapping)
        and number in [int(value) for value in group.get("numbers", [])]
    ]
    if len(matches) > 1:
        raise ValueError("%s has overlapping %s groups for question %d" % (source_id, kind, number))
    return matches[0] if matches else None


def _source_page_url(
    source: Mapping[str, Any], group: Optional[Mapping[str, Any]], *, stem: bool
) -> str:
    if group is not None:
        start = group.get("page_start")
        if start is not None:
            page_urls = source.get("page_urls")
            if isinstance(page_urls, Mapping) and str(start) in page_urls:
                return str(page_urls[str(start)])
            images = source.get("stem_image_urls" if stem else "answer_image_urls")
            if isinstance(images, list) and images:
                position = int(start) - 1 if stem else int(start) - 1
                if len(images) == 1:
                    position = 0
                if 0 <= position < len(images):
                    value = images[position]
                    return str(value.get("image_url", "")) if isinstance(value, Mapping) else str(value)
    return str(source.get("url", ""))


def _locator(group: Mapping[str, Any], number: int) -> str:
    template = str(group.get("locator_template") or "第{n}题")
    return template.replace("{n}", str(number))


def _canonical_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _evidence_for_group(
    source_pages: Sequence[Mapping[str, Any]],
    group: Optional[Mapping[str, Any]],
    *,
    stem: bool,
    number: int,
) -> List[Dict[str, Any]]:
    if group is None:
        return []
    start = group.get("page_start")
    if start is None:
        pages = [1]
    else:
        first = int(start)
        last = int(group.get("page_end", first))
        if first < 1 or last < first:
            raise ValueError("invalid evidence page range: %r" % group)
        pages = list(range(first, last + 1))
    wanted_scope = "stem" if stem else "answer"
    matches: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    for page in pages:
        candidates = [
            row
            for row in source_pages
            if page in [int(value) for value in row.get("covered_page_starts", [])]
            and number in [int(value) for value in row.get("question_numbers", [])]
            and str(row.get("scope"))
            in {wanted_scope, "stem_and_answer", "page", "document"}
        ]
        if not candidates:
            raise ValueError(
                "no frozen source page evidence for %s page %d"
                % ("stem" if stem else "answer", page)
            )
        for row in candidates:
            path = str(row["path"])
            if path in seen_paths:
                continue
            seen_paths.add(path)
            matches.append(
                {
                    "path": path,
                    "url": str(row["url"]),
                    "scope": str(row["scope"]),
                    "page_index": int(row["page_index"]),
                    "covered_page_starts": [
                        int(value) for value in row["covered_page_starts"]
                    ],
                    "sha256": str(row["sha256"]),
                    "bytes": int(row["bytes"]),
                }
            )
    if not matches:
        raise ValueError("empty frozen source page evidence group: %r" % group)
    return matches


def _source_assessment(
    *,
    source: Mapping[str, Any],
    item: Mapping[str, Any],
    role: str,
    stem_group: Optional[Mapping[str, Any]],
    answer_group: Optional[Mapping[str, Any]],
    canonical_text: str,
    source_pages: Sequence[Mapping[str, Any]],
    observation: Optional[Mapping[str, Any]] = None,
    override: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    number = int(item["number"])
    joint = role == "joint_supporting"
    stem_only = role == "stem_supporting"
    answer_only = role == "answer_only_supporting"
    rejected = role == "rejected"
    stem = str(item.get("stem") or item.get("content") or "").strip()
    requires_answer_observation = joint or answer_only
    if rejected:
        observed_answer = str((override or {}).get("observed_answer") or "").strip()
        observed_excerpt = str(
            (override or {}).get("observed_excerpt") or observed_answer
        ).strip()
        stem_match = str(
            (override or {}).get("stem_match") or "exact_stem_match"
        ).strip()
        answer_match = str(
            (override or {}).get("answer_match") or "external_answer_error"
        ).strip()
        normalized_answer = str(
            (override or {}).get("normalized_answer") or observed_answer
        ).strip()
        stem_equivalence_basis = str(
            (override or {}).get("stem_equivalence_basis") or ""
        ).strip()
        answer_equivalence_basis = str(
            (override or {}).get("answer_equivalence_basis") or ""
        ).strip()
        observed_stem = str((override or {}).get("observed_stem") or "").strip()
    elif requires_answer_observation:
        if not isinstance(observation, Mapping):
            raise ValueError(
                "source %s has no explicit answer observation for question %d"
                % (source["id"], number)
            )
        observed_answer = str(observation.get("value") or "").strip()
        observed_excerpt = str(observation.get("excerpt") or observed_answer).strip()
        stem_match = str(observation.get("stem_match") or "exact_stem_match").strip()
        answer_match = str(
            observation.get("answer_match") or "exact_answer_match"
        ).strip()
        normalized_answer = str(
            observation.get("normalized_value") or observed_answer
        ).strip()
        stem_equivalence_basis = str(
            observation.get("stem_equivalence_basis") or ""
        ).strip()
        answer_equivalence_basis = str(
            observation.get("answer_equivalence_basis") or ""
        ).strip()
        observed_stem = str(
            observation.get("stem") or observation.get("observed_stem") or ""
        ).strip()
        if not observed_answer or not observed_excerpt:
            raise ValueError(
                "source %s has an empty answer observation for question %d"
                % (source["id"], number)
            )
        if stem_match not in {"exact_stem_match", "equivalent_stem_match"}:
            raise ValueError(
                "source %s has invalid stem match for question %d: %s"
                % (source["id"], number, stem_match)
            )
        if stem_match == "equivalent_stem_match":
            if not observed_stem or not stem_equivalence_basis:
                raise ValueError(
                    "source %s has an equivalent stem without observed text and basis for question %d"
                    % (source["id"], number)
                )
        if answer_match not in {"exact_answer_match", "equivalent_answer_match"}:
            raise ValueError(
                "source %s has invalid answer match for question %d: %s"
                % (source["id"], number, answer_match)
            )
        if answer_match == "exact_answer_match" and observed_answer != canonical_text:
            raise ValueError(
                "source %s answer observation differs from canonical answer for question %d: %s != %s"
                % (source["id"], number, observed_answer, canonical_text)
            )
        if answer_match == "equivalent_answer_match":
            if normalized_answer != canonical_text:
                raise ValueError(
                    "source %s normalized answer does not reduce to canonical for question %d: %s != %s"
                    % (source["id"], number, normalized_answer, canonical_text)
                )
            if not answer_equivalence_basis:
                raise ValueError(
                    "source %s has an equivalent answer without a basis for question %d"
                    % (source["id"], number)
                )
    else:
        observed_answer = ""
        observed_excerpt = ""
        stem_match = "not_assessed" if stem_group is None else "exact_stem_match"
        answer_match = "not_assessed"
        normalized_answer = ""
        stem_equivalence_basis = ""
        answer_equivalence_basis = ""
        observed_stem = ""
    answer_note = observed_answer if (rejected or requires_answer_observation) else ""
    answer_excerpt = (
        observed_excerpt if (rejected or requires_answer_observation) else ""
    )
    source_answer_text = (
        observed_excerpt if (rejected or requires_answer_observation) else ""
    )
    rejection_reason = (
        str((override or {}).get("rejection_reason") or "").strip() if rejected else ""
    )
    final_stem_match = stem_match if (rejected or requires_answer_observation) else (
        "exact_stem_match" if stem_group is not None else "not_assessed"
    )
    if rejected and final_stem_match == "mismatched_stem" and not observed_stem:
        raise ValueError(
            "source %s has a mismatched stem for question %d but no observed stem text"
            % (source["id"], number)
        )
    supports_stem = bool(stem_group is not None) and final_stem_match not in {
        "mismatched_stem",
        "not_assessed",
    }
    supports_canonical = bool(joint or answer_only)
    stem_evidence = _evidence_for_group(
        source_pages, stem_group, stem=True, number=number
    )
    answer_evidence = _evidence_for_group(
        source_pages, answer_group, stem=False, number=number
    )
    stem_page_start = (
        int(stem_group.get("page_start") or 1) if stem_group is not None else None
    )
    stem_page_end = (
        int(stem_group.get("page_end") or stem_page_start)
        if stem_group is not None
        else None
    )
    answer_page_start = (
        int(answer_group.get("page_start") or 1) if answer_group is not None else None
    )
    answer_page_end = (
        int(answer_group.get("page_end") or answer_page_start)
        if answer_group is not None
        else None
    )
    return {
        "source_id": source["id"],
        "publisher": source["publisher"],
        "url": source["url"],
        "stem_page_url": (
            stem_evidence[0]["url"]
            if stem_evidence
            else _source_page_url(source, stem_group, stem=True)
            if stem_group is not None
            else ""
        ),
        "answer_page_url": (
            answer_evidence[0]["url"]
            if answer_evidence
            else _source_page_url(source, answer_group, stem=False)
            if answer_group is not None
            else ""
        ),
        "stem_evidence": stem_evidence,
        "answer_evidence": answer_evidence,
        "stem_page_start": stem_page_start,
        "stem_page_end": stem_page_end,
        "answer_page_start": answer_page_start,
        "answer_page_end": answer_page_end,
        "access_method": source.get("access_method") or "打开来源页面并逐题核对",
        "independence_group": source["independence_group"],
        "evidence_scope": source["evidence_scope"],
        "role": role,
        "supports_stem": supports_stem,
        "supports_canonical": supports_canonical,
        "stem_match": final_stem_match,
        "answer_match": answer_match,
        "stem_locator": _locator(stem_group, number) if stem_group is not None else "",
        "answer_locator": (
            str((observation or {}).get("locator") or "").strip()
            or _locator(answer_group, number)
            if answer_group is not None
            else ""
        ),
        "stem_excerpt": observed_stem or stem,
        "observed_stem": observed_stem,
        "stem_equivalence_basis": stem_equivalence_basis,
        "answer_value": answer_note,
        "answer_excerpt": answer_excerpt,
        "source_answer_text": source_answer_text,
        "normalized_answer": normalized_answer if supports_canonical else "",
        "answer_equivalence_basis": answer_equivalence_basis if supports_canonical else "",
        "rejection_reason": rejection_reason or None,
    }


def build_source_audit(
    *,
    spec: Mapping[str, Any],
    audit_spec: Mapping[str, Any],
    source_pages: Mapping[str, Sequence[Mapping[str, Any]]],
    items: Sequence[Mapping[str, Any]],
    answer_keys: Mapping[str, Mapping[str, Any]],
    paper_id: str,
    allow_incomplete: bool,
) -> tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    policy = audit_spec["policy"]
    required_stem = int(policy["required_stem_independent_groups"])
    required_joint = int(policy["required_joint_answer_independent_groups"])
    sources = {
        str(source["id"]): source
        for source in audit_spec["sources"]
        if isinstance(source, Mapping) and source.get("id")
    }
    primary_id = str(spec["source"]["id"])
    if primary_id not in sources:
        raise ValueError("primary source is not registered in the source audit spec")
    coverage = audit_spec["coverage"]
    required_ids = [str(item["id"]) for item in items]
    if len(required_ids) != int(policy.get("required_question_count", len(required_ids))):
        raise ValueError("source audit policy question count does not match catalogue")

    rows: List[Dict[str, Any]] = []
    by_question: Dict[str, Dict[str, Any]] = {}
    for item in items:
        qid = str(item["id"])
        number = int(item["number"])
        canonical = (
            answer_keys[qid]["canonical"]
            if qid in answer_keys
            else item.get("reference_answer")
        )
        canonical_text = _canonical_text(canonical)
        assessments: List[Dict[str, Any]] = []

        primary = sources[primary_id]
        primary_group = {
            "page_start": int(item["page_number"]),
            "page_end": int(item["page_number"]),
            "locator_template": "冻结 PDF 第%d页；第%d题" % (int(item["page_number"]), number),
        }
        assessments.append(
            _source_assessment(
                source=primary,
                item=item,
                role="stem_supporting",
                stem_group=primary_group,
                answer_group=None,
                canonical_text=canonical_text,
                source_pages=source_pages.get(primary_id, ()),
            )
        )

        for source_id, source_coverage in coverage.items():
            if source_id not in sources:
                raise ValueError("coverage references unknown source: %s" % source_id)
            if source_id == primary_id:
                continue
            source = sources[source_id]
            stem_group = _group_for_number(
                source_coverage.get("stem_groups"), number, source_id=source_id, kind="stem"
            )
            answer_group = _group_for_number(
                source_coverage.get("joint_answer_groups"),
                number,
                source_id=source_id,
                kind="joint_answer",
            )
            answer_only_group = _group_for_number(
                source_coverage.get("answer_groups"),
                number,
                source_id=source_id,
                kind="answer_only",
            )
            overrides = source_coverage.get("question_overrides")
            override = (
                overrides.get(str(number)) if isinstance(overrides, Mapping) else None
            )
            observations = source_coverage.get("observed_answers")
            observation = (
                observations.get(str(number))
                if isinstance(observations, Mapping)
                else None
            )
            if override is not None:
                if not isinstance(override, Mapping) or override.get("role") != "rejected":
                    raise ValueError(
                        "question override must be a rejected assessment for %s/%s"
                        % (source_id, number)
                    )
                if stem_group is None or answer_group is None:
                    raise ValueError(
                        "rejected source override needs stem and answer groups for %s/%s"
                        % (source_id, number)
                    )
                if not str(override.get("observed_answer") or "").strip():
                    raise ValueError(
                        "rejected source override has no observed answer for %s/%s"
                        % (source_id, number)
                    )
                if not str(override.get("rejection_reason") or "").strip():
                    raise ValueError(
                        "rejected source override has no rejection reason for %s/%s"
                        % (source_id, number)
                    )
                role = "rejected"
                selected_answer = answer_group
            elif source.get("evidence_scope") == "stem_and_answer":
                if stem_group is None:
                    continue
                if answer_group is None:
                    role = "stem_supporting"
                    selected_answer = None
                else:
                    role = "joint_supporting"
                    selected_answer = answer_group
            elif source.get("evidence_scope") == "stem_only":
                if stem_group is None:
                    continue
                role = "stem_supporting"
                selected_answer = None
            elif source.get("evidence_scope") == "answer_only":
                if answer_only_group is None:
                    continue
                role = "answer_only_supporting"
                stem_group = None
                selected_answer = answer_only_group
            else:
                raise ValueError("unsupported evidence scope for %s" % source_id)
            assessments.append(
                _source_assessment(
                    source=source,
                    item=item,
                    role=role,
                    stem_group=stem_group,
                    answer_group=selected_answer,
                    canonical_text=canonical_text,
                    source_pages=source_pages.get(source_id, ()),
                    observation=observation if isinstance(observation, Mapping) else None,
                    override=override if isinstance(override, Mapping) else None,
                )
            )

        stem_groups = {
            assessment["independence_group"]
            for assessment in assessments
            if assessment["supports_stem"]
            and assessment["stem_match"] in {"exact_stem_match", "equivalent_stem_match"}
        }
        joint_groups = {
            assessment["independence_group"]
            for assessment in assessments
            if assessment["role"] == "joint_supporting"
            and assessment["supports_stem"]
            and assessment["supports_canonical"]
            and assessment["evidence_scope"] == "stem_and_answer"
            and assessment["stem_match"] in {"exact_stem_match", "equivalent_stem_match"}
            and assessment["answer_match"] in {"exact_answer_match", "equivalent_answer_match"}
        }
        stem_complete = len(stem_groups) >= required_stem
        joint_complete = len(joint_groups) >= required_joint
        row = {
            "schema_version": SOURCE_AUDIT_SCHEMA_VERSION,
            "dataset_version": spec["dataset_version"],
            "question_id": qid,
            "paper_id": paper_id,
            "question_type": item["type"],
            "points": int(item["points"]),
            "canonical_answer": canonical_text,
            "stem_source_ids": [
                assessment["source_id"]
                for assessment in assessments
                if assessment["supports_stem"]
            ],
            "joint_answer_source_ids": [
                assessment["source_id"]
                for assessment in assessments
                if assessment["role"] == "joint_supporting"
            ],
            "answer_only_source_ids": [
                assessment["source_id"]
                for assessment in assessments
                if assessment["role"] == "answer_only_supporting"
            ],
            "rejected_source_ids": [
                assessment["source_id"]
                for assessment in assessments
                if assessment["role"] == "rejected"
            ],
            "stem_independent_groups": len(stem_groups),
            "joint_answer_independent_groups": len(joint_groups),
            "stem_dual_source_complete": stem_complete,
            "joint_answer_dual_source_complete": joint_complete,
            "gate_complete": stem_complete and joint_complete,
            "evidence_paths": sorted(
                {
                    str(evidence["path"])
                    for assessment in assessments
                    for key in ("stem_evidence", "answer_evidence")
                    for evidence in assessment.get(key, [])
                }
            ),
            "assessments": assessments,
        }
        rows.append(row)
        by_question[qid] = row

    stem_pass = sum(1 for row in rows if row["stem_dual_source_complete"])
    joint_pass = sum(1 for row in rows if row["joint_answer_dual_source_complete"])
    gate_pass = sum(1 for row in rows if row["gate_complete"])
    summary = {
        "policy": dict(policy),
        "questions": len(rows),
        "stem_dual_source_questions": stem_pass,
        "joint_answer_dual_source_questions": joint_pass,
        "gate_complete_questions": gate_pass,
        "evidence_ready": gate_pass == len(rows),
        "gate": "pass" if gate_pass == len(rows) else "blocked",
        "failed_question_ids": [row["question_id"] for row in rows if not row["gate_complete"]],
        "evidence_page_count": len(
            {
                str(evidence["path"])
                for row in rows
                for assessment in row["assessments"]
                for key in ("stem_evidence", "answer_evidence")
                for evidence in assessment.get(key, [])
            }
        ),
    }
    if not summary["evidence_ready"] and not allow_incomplete:
        raise ValueError(
            "dual-source gate failed for %d questions: %s"
            % (len(summary["failed_question_ids"]), ", ".join(summary["failed_question_ids"]))
        )
    return rows, by_question, summary


def resolve_pdf(pdf: Path | None, cache_dir: Path, source: Mapping[str, Any]) -> Path:
    if pdf is None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pdf = cache_dir / "2026-national-1-math.pdf"
        if not pdf.is_file():
            request = urllib.request.Request(
                str(source["url"]), headers={"User-Agent": "Gaokao-Bench/0.1"}
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                pdf.write_bytes(response.read())
    pdf = pdf.resolve()
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    actual = sha256(pdf)
    if actual != source["sha256"]:
        raise ValueError(
            "source PDF hash mismatch: expected %s, got %s" % (source["sha256"], actual)
        )
    return pdf


def render_crop(pdf_path: Path, item: Mapping[str, Any], destination: Path) -> None:
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover - build-time dependency
        raise RuntimeError("building crops requires PyMuPDF") from exc

    page_number = int(item["page_number"])
    bbox = [float(value) for value in item["bbox"]]
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("invalid crop bbox for %s" % item["id"])
    with fitz.open(pdf_path) as document:
        if not 1 <= page_number <= document.page_count:
            raise ValueError("page outside PDF for %s" % item["id"])
        page = document[page_number - 1]
        rect = fitz.Rect(*bbox)
        if not rect.intersects(page.rect):
            raise ValueError("crop does not intersect page for %s" % item["id"])
        pixmap = page.get_pixmap(
            clip=rect, matrix=fitz.Matrix(2.2, 2.2), colorspace=fitz.csRGB, alpha=False
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(destination))


def answer_key_row(
    item: Mapping[str, Any], qid: str, paper_id: str, audited: bool
) -> Dict[str, Any]:
    is_choice = item["type"] in {"single_choice", "multi_choice"}
    slots = []
    if not is_choice:
        for raw in item["slots"]:
            slot = {
                "canonical": str(raw["canonical"]),
                "matcher": str(raw.get("matcher", "numeric")),
                "tolerance": float(raw.get("tolerance", 1e-9)),
                "value": float(raw["value"]),
            }
            slots.append(slot)
    return {
        "schema_version": SCHEMA_VERSION,
        "question_id": qid,
        "paper_id": paper_id,
        "points": int(item["points"]),
        "kind": item["type"],
        "correct_options": sorted(item.get("correct_options", [])),
        "slots": slots,
        "custom_validator": None,
        "canonical": item["canonical"],
        "answer_status": (
            "three_way_audit_passed_pending_owner_signoff"
            if audited
            else "source_and_derivation_confirmed_pending_three_way_audit"
        ),
        "notes": None,
    }


def question_row(
    item: Mapping[str, Any],
    paper: Mapping[str, Any],
    source: Mapping[str, Any],
    image_path: str,
    image_sha256: str,
    image_bytes: int,
    audited: bool,
) -> Dict[str, Any]:
    qid = str(item["id"])
    stem = str(item["stem"]).strip()
    options = [dict(option) for option in item.get("options", [])]
    if options:
        content = stem + " " + " ".join(
            "(%s) %s" % (option["label"], option["text"]) for option in options
        )
    else:
        content = stem
    return {
        "schema_version": SCHEMA_VERSION,
        "id": qid,
        "paper_id": paper["id"],
        "number": int(item["number"]),
        "display_number": "第%d题" % int(item["number"]),
        "type": item["type"],
        "points": int(item["points"]),
        "stem": stem,
        "content": content,
        "content_format": "unicode_math_plain_text",
        "options": options,
        "answer_slot_count": 1 if options else len(item["slots"]),
        "asset_dependency": item["asset_dependency"],
        "image_path": image_path,
        "image_sha256": image_sha256,
        "image_bytes": image_bytes,
        "input_modalities": ["text"],
        "owner_attested": False,
        "review_status": (
            "three_way_audit_passed_pending_owner_signoff"
            if audited
            else "pdf_text_and_crop_verified_pending_three_way_audit"
        ),
        "source": {
            "source_id": source["id"],
            "source_url": source["url"],
            "source_pdf_sha256": source["sha256"],
            "page_number": int(item["page_number"]),
            "bbox": [float(value) for value in item["bbox"]],
            "crop_method": CROP_METHOD,
            "evidence_status": "pdf_text_anchor_crop_sha256_verified",
        },
    }


def subjective_row(
    item: Mapping[str, Any],
    paper_id: str,
    source: Mapping[str, Any],
    asset: Mapping[str, Any],
    audited: bool,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": item["id"],
        "paper_id": paper_id,
        "number": int(item["number"]),
        "display_number": "第%d题" % int(item["number"]),
        "type": "catalog_subjective",
        "points": int(item["points"]),
        "content": item["content"].strip(),
        "reference_answer": item["reference_answer"].strip(),
        "solution_outline": item["solution_outline"].strip(),
        "answer_source_id": item.get(
            "answer_source_id", "src-gk100-2026-n1-answers"
        ),
        "secondary_answer_source_id": item.get("secondary_answer_source_id"),
        "answer_locator": item.get(
            "answer_locator", "完整解析；第%d题" % int(item["number"])
        ),
        "secondary_answer_locator": item.get("secondary_answer_locator", ""),
        "secondary_reference_answer": item.get("secondary_reference_answer"),
        "point_evidence_status": item.get(
            "point_evidence_status", "full_paper_section_and_points_crosschecked"
        ),
        "point_locator": item.get(
            "point_locator", "试卷结构；解答题第%d题，%d分" % (int(item["number"]), int(item["points"]))
        ),
        "answer_status": (
            "three_way_audit_passed_pending_owner_signoff"
            if audited
            else "source_and_derivation_confirmed_pending_three_way_audit"
        ),
        "reference_answer_status": (
            "three_way_audit_passed_pending_owner_signoff"
            if audited
            else "source_and_derivation_confirmed_pending_three_way_audit"
        ),
        "included_in_scoring": False,
        "deferred_reason": (
            "主观题需要锁定评分细则并与人工裁决绑定；当前收录完整题面、参考答案和解答要点，"
            "不作为确定性自动评分分母。"
        ),
        "asset_dependency": item["asset_dependency"],
        "image_path": asset["path"],
        "image_sha256": asset["sha256"],
        "image_bytes": asset["bytes"],
        "source_id": source["id"],
        "source_pdf_sha256": source["sha256"],
        "page_number": int(item["page_number"]),
        "bbox": [float(value) for value in item["bbox"]],
    }


def validate_audit_reports(
    reports: Sequence[Path], spec: Mapping[str, Any], candidate_manifest_sha256: Optional[str]
) -> List[Dict[str, Any]]:
    if not reports:
        return []
    if not candidate_manifest_sha256:
        raise ValueError(
            "audit reports require --candidate-manifest-sha256 from an unaudited staging build"
        )
    if len(reports) != 3:
        raise ValueError("exactly three audit reports are required")
    expected_ids = [item["id"] for item in spec["objective"]] + [
        item["id"] for item in spec["subjective"]
    ]
    expected_audits = {"A", "B", "C"}
    normalized: List[Dict[str, Any]] = []
    for path in reports:
        report = read_json(path)
        audit_id = str(report.get("audit_id", "")).strip()
        if audit_id not in expected_audits:
            raise ValueError("invalid audit_id in %s" % path)
        if report.get("dataset_version") != spec["dataset_version"]:
            raise ValueError("audit report version mismatch: %s" % path)
        if report.get("dataset_manifest_sha256") != candidate_manifest_sha256:
            raise ValueError("audit report is not bound to the current candidate manifest: %s" % path)
        checked = report.get("checked_question_ids")
        if not isinstance(checked, list) or sorted(checked) != sorted(expected_ids):
            raise ValueError("audit report does not cover all 19 questions: %s" % path)
        if report.get("verdict") not in {"pass", "fail"}:
            raise ValueError("audit report verdict must be pass or fail: %s" % path)
        issues = report.get("issues", [])
        if not isinstance(issues, list):
            raise ValueError("audit issues must be a list: %s" % path)
        if report["verdict"] == "pass" and issues:
            raise ValueError("passing audit report contains issues: %s" % path)
        normalized.append(
            {
                "audit_id": audit_id,
                "verdict": report["verdict"],
                "issues": issues,
                "report_path": str(path.resolve()),
                "report_sha256": sha256(path),
                "candidate_manifest_sha256": candidate_manifest_sha256,
                "checked_questions": len(checked),
            }
        )
    if {row["audit_id"] for row in normalized} != expected_audits:
        raise ValueError("audit reports must contain exactly one A, B, and C report")
    if any(row["verdict"] != "pass" for row in normalized):
        raise ValueError("all three audit reports must pass before a candidate can be marked audited")
    return sorted(normalized, key=lambda row: row["audit_id"])


def build(args: argparse.Namespace) -> Dict[str, Any]:
    spec = read_json(Path(args.spec).resolve())
    audit_spec = load_source_audit_spec(spec)
    source_pages_spec = load_source_pages_spec(spec, audit_spec)
    source_policy = audit_spec["policy"]
    required_stem = int(source_policy["required_stem_independent_groups"])
    required_joint = int(source_policy["required_joint_answer_independent_groups"])
    source = spec["source"]
    paper_spec = spec["paper"]
    version = str(spec["dataset_version"])
    out_dir = Path(args.out).resolve()
    revision = source_revision()
    spec_path = Path(args.spec).resolve()
    source_audit_spec_path = (ROOT / str(spec["source_audit_spec"])).resolve()
    source_pages_spec_path = (ROOT / str(spec["source_pages_spec"])).resolve()
    if worktree_is_dirty():
        raise ValueError(
            "refusing to build a release candidate from a dirty worktree; "
            "commit the builder, specs, and source-audit spec first"
        )
    if out_dir == ROOT or out_dir == Path.home():
        raise ValueError("refusing unsafe output directory")
    if out_dir.exists() and not out_dir.is_dir():
        raise ValueError("refusing non-directory output path: %s" % out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ValueError(
            "refusing to overwrite an existing data package: %s; "
            "choose a new immutable --out path and dataset version" % out_dir
        )
    if out_dir.exists():
        out_dir.rmdir()
    pdf_path = resolve_pdf(args.pdf, Path(args.cache_dir).resolve(), source)
    prior_manifest = out_dir / "manifest.json"
    prior_manifest_sha256 = sha256(prior_manifest) if prior_manifest.is_file() else None
    candidate_manifest_sha256 = (
        str(args.candidate_manifest_sha256).strip()
        if getattr(args, "candidate_manifest_sha256", None)
        else prior_manifest_sha256
    )
    audit_reports = validate_audit_reports(
        [Path(value).resolve() for value in args.audit_report], spec, candidate_manifest_sha256
    )
    audited = bool(audit_reports)

    temporary = out_dir.with_name(out_dir.name + ".building")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    assets_dir = temporary / "assets" / paper_spec["id"]
    catalog_dir = temporary / "catalog"
    examples_dir = temporary / "examples"
    pinned_spec_dir = temporary / "audit" / "specs"
    for directory in (assets_dir, catalog_dir, examples_dir, pinned_spec_dir):
        directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_audit_spec_path, pinned_spec_dir / "source-audit.json")
    shutil.copyfile(source_pages_spec_path, pinned_spec_dir / "source-pages.json")
    if audit_reports:
        audit_report_dir = temporary / "audit_reports"
        audit_report_dir.mkdir(parents=True)
        packaged_audit_reports: List[Dict[str, Any]] = []
        for report in audit_reports:
            source_path = Path(report["report_path"])
            report_relative = "audit_reports/audit-%s.json" % report["audit_id"].lower()
            shutil.copyfile(
                source_path,
                temporary / report_relative,
            )
            markdown_path = source_path.with_suffix(".md")
            if markdown_path.is_file():
                shutil.copyfile(
                    markdown_path,
                    audit_report_dir / ("audit-%s.md" % report["audit_id"].lower()),
                )
            packaged = dict(report)
            packaged["report_path"] = report_relative
            packaged_audit_reports.append(packaged)
    else:
        packaged_audit_reports = []

    source_pages_rows, source_pages_by_source = materialize_source_pages(
        source_pages_spec=source_pages_spec,
        cache_dir=Path(args.source_page_cache).resolve(),
        output_dir=temporary,
    )
    write_jsonl(temporary / "source_pages.jsonl", source_pages_rows)

    paper_id = paper_spec["id"]
    all_items = list(spec["objective"]) + list(spec["subjective"])
    assets: List[Dict[str, Any]] = []
    image_records: Dict[str, Dict[str, Any]] = {}
    for item in all_items:
        qid = item["id"]
        filename = "q-%02d.png" % int(item["number"])
        relative = "assets/%s/%s" % (paper_id, filename)
        destination = temporary / relative
        render_crop(pdf_path, item, destination)
        record = {
            "question_id": qid,
            "paper_id": paper_id,
            "path": relative,
            "sha256": sha256(destination),
            "bytes": destination.stat().st_size,
            "dependency": item["asset_dependency"],
            "page_number": int(item["page_number"]),
            "bbox": [float(value) for value in item["bbox"]],
            "crop_method": CROP_METHOD,
            "source_pdf_sha256": source["sha256"],
        }
        assets.append(record)
        image_records[qid] = record

    questions = [
        question_row(
            item,
            paper_spec,
            source,
            image_records[item["id"]]["path"],
            image_records[item["id"]]["sha256"],
            image_records[item["id"]]["bytes"],
            audited,
        )
        for item in spec["objective"]
    ]
    answer_keys = [
        answer_key_row(item, item["id"], paper_id, audited) for item in spec["objective"]
    ]
    subjective = [
        subjective_row(item, paper_id, source, image_records[item["id"]], audited)
        for item in spec["subjective"]
    ]
    objective_keys_by_id = {row["question_id"]: row for row in answer_keys}
    source_audit_rows, source_audit_by_question, source_audit_summary = build_source_audit(
        spec=spec,
        audit_spec=audit_spec,
        source_pages=source_pages_by_source,
        items=all_items,
        answer_keys=objective_keys_by_id,
        paper_id=paper_id,
        allow_incomplete=bool(getattr(args, "allow_incomplete_source_audit", False)),
    )

    objective_points = sum(int(item["points"]) for item in spec["objective"])
    subjective_points = sum(int(item["points"]) for item in spec["subjective"])
    objective_image_questions = len(
        [item for item in spec["objective"] if item["asset_dependency"] == "required"]
    )
    subjective_image_questions = len(
        [item for item in spec["subjective"] if item["asset_dependency"] == "required"]
    )
    full_paper_image_questions = objective_image_questions + subjective_image_questions
    if objective_points != 73 or subjective_points != 77:
        raise ValueError("point structure must close at 73 objective + 77 subjective")
    if objective_points + subjective_points != 150:
        raise ValueError("full paper must close at 150 points")

    status = "audited_pending_owner_signoff" if audited else "pending_three_way_independent_audit"
    legacy_sources = {
        str(row["id"]): row for row in spec.get("answer_sources", []) if row.get("id")
    }
    answer_sources: List[Dict[str, Any]] = []
    registered_source_ids: set[str] = set()
    for row in audit_spec["sources"]:
        source_id = str(row["id"])
        registered_source_ids.add(source_id)
        source_coverage = audit_spec["coverage"].get(source_id, {})
        answer_image_pages: Dict[str, int] = {}
        for group in (
            list(source_coverage.get("joint_answer_groups", []))
            + list(source_coverage.get("answer_groups", []))
        ):
            if not isinstance(group, Mapping) or group.get("page_start") is None:
                continue
            for number in group.get("numbers", []):
                answer_image_pages[str(number)] = int(group["page_start"])
        stem_image_pages: Dict[str, int] = {}
        if len(row.get("stem_image_urls") or []) > 1:
            for group in source_coverage.get("stem_groups", []):
                if not isinstance(group, Mapping) or group.get("page_start") is None:
                    continue
                for number in group.get("numbers", []):
                    stem_image_pages[str(number)] = int(group["page_start"])
        answer_sources.append(
            {
                **row,
                **({"answer_image_pages": answer_image_pages} if answer_image_pages else {}),
                **({"stem_image_pages": stem_image_pages} if stem_image_pages else {}),
                "paper_id": paper_id,
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
                "rights_status": "cc_by_4_0_public_release",
                "redistribution": "cleared_with_attribution",
            }
        )
    for source_id, row in legacy_sources.items():
        if source_id in registered_source_ids:
            continue
        answer_sources.append(
            {
                **row,
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
                "rights_status": "cc_by_4_0_public_release",
                "redistribution": "cleared_with_attribution",
            }
        )
    answer_sources_by_id = {str(row["id"]): row for row in answer_sources}
    answer_audit: List[Dict[str, Any]] = []
    question_audit: List[Dict[str, Any]] = []
    point_audit: List[Dict[str, Any]] = []
    asset_audit: List[Dict[str, Any]] = []
    for item, key in zip(spec["objective"], answer_keys):
        qid = item["id"]
        audit_row = source_audit_by_question[qid]
        assessments = audit_row["assessments"]
        primary_assessment = next(
            assessment
            for assessment in assessments
            if assessment["source_id"] == "src-gk100-2026-n1-answers"
        )
        secondary_assessment = next(
            assessment
            for assessment in assessments
            if assessment["source_id"] == "src-rendoc-2026-n1-full"
        )
        supporting = [
            assessment["source_id"]
            for assessment in assessments
            if assessment["role"] == "answer_only_supporting"
        ]
        answer_audit.append(
            {
                "schema_version": SCHEMA_VERSION,
                "question_id": qid,
                "paper_id": paper_id,
                "question_type": item["type"],
                "reviewed_at": spec["created_at"],
                "reviewer": "Gaokao-Bench deterministic build",
                "current_canonical": item["canonical"],
                "reviewed_canonical": item["canonical"],
                "external_canonical": item["canonical"],
                "external_source_id": primary_assessment["source_id"],
                "external_locator": primary_assessment["answer_locator"],
                "external_excerpt": primary_assessment.get("source_answer_text")
                or primary_assessment["answer_excerpt"],
                "secondary_source_id": secondary_assessment["source_id"],
                "secondary_external_canonical": item["canonical"],
                "secondary_source_locator": secondary_assessment["answer_locator"],
                "supporting_source_ids": supporting,
                "rejected_source_ids": audit_row.get("rejected_source_ids", []),
                "match_method": "explicit_answer_and_independent_derivation",
                "source_audit_gate": audit_row["gate_complete"],
                "source_audit_stem_groups": audit_row["stem_independent_groups"],
                "source_audit_joint_answer_groups": audit_row[
                    "joint_answer_independent_groups"
                ],
                "decision": "retain_current",
                "status": "audited_passed" if audited else "source_and_derivation_confirmed",
                "pre_attestation_status": "source_and_derivation_crosschecked_pending_three_way_audit",
                "owner_attested": False,
                "human_independent_review": "pending",
            }
        )
        question_audit.append(
            {
                "schema_version": SCHEMA_VERSION,
                "question_id": qid,
                "paper_id": paper_id,
                "number": int(item["number"]),
                "source_pdf_sha256": source["sha256"],
                "page_number": int(item["page_number"]),
                "bbox": [float(value) for value in item["bbox"]],
                "stem_verdict": "pdf_exact",
                "option_verdict": "pdf_exact" if item["options"] else "not_applicable",
                "asset_verdict": "figure_required" if item["asset_dependency"] == "required" else "text_sufficient",
                "answer_verdict": "confirmed",
                "answer_canonical": key["canonical"],
                "derivation": item["derivation"],
                "review_status": "audited_passed" if audited else "independently_derived_pending_owner_signoff",
                "reviewer": "Gaokao-Bench deterministic build; three-way audit pending",
                "reviewed_at": spec["created_at"],
            }
        )
        point_audit.append(
            {
                "schema_version": SCHEMA_VERSION,
                "item_id": qid,
                "question_id": qid,
                "paper_id": paper_id,
                "number": int(item["number"]),
                "kind": item["type"],
                "current_points": int(item["points"]),
                "reviewed_points": int(item["points"]),
                "previous_points": None,
                "delta": 0,
                "lifetime_delta": 0,
                "source_ids": sorted(
                    {source["id"]}
                    | {str(assessment["source_id"]) for assessment in assessments}
                ),
                "locator": "2026 全国卷 I 数学试卷结构；第%d题" % int(item["number"]),
                "match_method": "official_paper_section_and_question_points",
                "decision": "retain_current",
                "status": "audited_passed" if audited else "source_points_crosschecked_pending_owner_signoff",
                "pre_attestation_status": "source_points_crosschecked_pending_owner_signoff",
                "owner_attested": False,
                "human_independent_review": "pending",
                "reviewer": "Gaokao-Bench deterministic build",
                "reviewed_at": spec["created_at"],
            }
        )
    for asset in assets:
        asset_audit.append(
            {
                "schema_version": SCHEMA_VERSION,
                "question_id": asset["question_id"],
                "paper_id": paper_id,
                "before_path": None,
                "before_sha256": None,
                "before_bytes": None,
                "after_path": asset["path"],
                "after_sha256": asset["sha256"],
                "after_bytes": asset["bytes"],
                "source_pdf_sha256": source["sha256"],
                "page_number": asset["page_number"],
                "bbox": asset["bbox"],
                "crop_method": CROP_METHOD,
                "anchor_text": next(
                    item.get("stem") or item.get("content")
                    for item in all_items
                    if item["id"] == asset["question_id"]
                )[:80],
                "status": "three_way_audit_passed_pending_owner_visual_signoff" if audited else "pending_visual_audit",
                "pre_attestation_status": "rendered_from_frozen_pdf_pending_visual_review",
                "decision": "retain_current",
                "reason": "Every crop is rendered deterministically from an explicit bbox on the frozen source PDF.",
                "owner_attested": False,
                "human_independent_review": "pending",
                "reviewer": "Gaokao-Bench deterministic build",
                "reviewed_at": spec["created_at"],
            }
        )

    paper = {
        "schema_version": SCHEMA_VERSION,
        "id": paper_id,
        "title": paper_spec["title"],
        "region": paper_spec["region"],
        "year": int(paper_spec["year"]),
        "subject": paper_spec["subject"],
        "source_id": source["id"],
        "source_url": source["url"],
        "source_pdf_sha256": source["sha256"],
        "full_question_count": len(all_items),
        "objective_questions": len(spec["objective"]),
        "choice_questions": len([item for item in spec["objective"] if item["type"] in {"single_choice", "multi_choice"}]),
        "single_choice_questions": len([item for item in spec["objective"] if item["type"] == "single_choice"]),
        "multi_choice_questions": len([item for item in spec["objective"] if item["type"] == "multi_choice"]),
        "fill_questions": len([item for item in spec["objective"] if item["type"] == "fill_blank"]),
        "objective_points": objective_points,
        "catalog_subjective_questions": len(spec["subjective"]),
        "catalog_subjective_points": subjective_points,
        "full_paper_points": int(paper_spec["full_paper_points"]),
        "scoring_scope": "choice_and_fill_only",
    }
    coverage = [
        {
            "paper_id": paper_id,
            "paper_title": paper_spec["title"],
            "choice_questions": paper["choice_questions"],
            "multi_choice_questions": paper["multi_choice_questions"],
            "fill_questions": paper["fill_questions"],
            "objective_questions": paper["objective_questions"],
            "objective_points": objective_points,
            "catalog_subjective_questions": paper["catalog_subjective_questions"],
            "catalog_subjective_points": subjective_points,
            "full_questions": paper["full_question_count"],
            "full_points": paper["full_paper_points"],
            "image_required_questions": full_paper_image_questions,
            "objective_image_required_questions": objective_image_questions,
            "catalog_subjective_image_required_questions": subjective_image_questions,
        },
        {
            "paper_id": "TOTAL",
            "paper_title": "2026 全国卷 I 数学",
            "choice_questions": paper["choice_questions"],
            "multi_choice_questions": paper["multi_choice_questions"],
            "fill_questions": paper["fill_questions"],
            "objective_questions": paper["objective_questions"],
            "objective_points": objective_points,
            "catalog_subjective_questions": paper["catalog_subjective_questions"],
            "catalog_subjective_points": subjective_points,
            "full_questions": paper["full_question_count"],
            "full_points": paper["full_paper_points"],
            "image_required_questions": full_paper_image_questions,
            "objective_image_required_questions": objective_image_questions,
            "catalog_subjective_image_required_questions": subjective_image_questions,
        },
    ]

    write_json(temporary / "benchmark_spec.json", spec["benchmark_spec"])
    write_jsonl(temporary / "papers.jsonl", [paper])
    write_jsonl(temporary / "questions.jsonl", questions)
    write_jsonl(temporary / "answer_keys.jsonl", answer_keys)
    write_jsonl(temporary / "catalog" / "subjective_catalog.jsonl", subjective)
    write_jsonl(temporary / "assets.jsonl", assets)
    source_registry_rows: List[Dict[str, Any]] = []
    for row in answer_sources:
        registry_row = {
            **row,
            "paper_id": paper_id,
            "source_type": row.get("source_type", "crosscheck_source"),
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
        }
        if row.get("id") == source["id"]:
            registry_row.update(
                {
                    "pdf_sha256": source["sha256"],
                    "page_count": int(source["page_count"]),
                    "notes": "Text-native exam paper; the source file contains no answer key.",
                }
            )
        source_registry_rows.append(registry_row)
    write_jsonl(temporary / "sources.jsonl", source_registry_rows)
    write_jsonl(temporary / "answer_sources.jsonl", answer_sources)
    write_jsonl(temporary / "source_audit.jsonl", source_audit_rows)
    write_jsonl(temporary / "answer_audit.jsonl", answer_audit)
    write_jsonl(temporary / "question_audit.jsonl", question_audit)
    write_jsonl(temporary / "point_audit.jsonl", point_audit)
    write_jsonl(temporary / "asset_audit.jsonl", asset_audit)
    write_jsonl(temporary / "reviews.jsonl", [])
    with (temporary / "coverage.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(coverage[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(coverage)

    gold = {"answers": {item["id"]: item["canonical"] for item in spec["objective"]}}
    write_json(examples_dir / "gold.json", gold)
    write_json(examples_dir / "empty.json", {"answers": {}})
    write_json(
        examples_dir / "malformed.json",
        {"answers": {spec["objective"][0]["id"]: "不是选项", spec["objective"][11]["id"]: ""}},
    )
    (examples_dir / "README.md").write_text(
        "# Examples\n\n"
        "`gold.json` is a regression fixture for the 14 objective questions. "
        "It is not an answer prompt. The five subjective questions are catalogued "
        "with reference answers and solution outlines but are not auto-scored.\n",
        encoding="utf-8",
    )

    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", temporary / "THIRD_PARTY_NOTICES.md")
    shutil.copyfile(ROOT / "LICENSE-DATA", temporary / "LICENSE-DATA")

    minimum_stem_groups = min(row["stem_independent_groups"] for row in source_audit_rows)
    minimum_joint_groups = min(
        row["joint_answer_independent_groups"] for row in source_audit_rows
    )
    stem_publishers = sorted(
        {
            str(row["publisher"])
            for row in answer_sources
            if row.get("evidence_scope") in {"stem_only", "stem_and_answer"}
        }
    )
    joint_publishers = sorted(
        {
            str(row["publisher"])
            for row in answer_sources
            if row.get("evidence_scope") == "stem_and_answer"
        }
    )
    answer_only_publishers = sorted(
        {
            str(row["publisher"])
            for row in answer_sources
            if row.get("evidence_scope") == "answer_only"
        }
    )
    rejected_source_count = sum(len(row["rejected_source_ids"]) for row in source_audit_rows)
    audit_summary = "三方独立审计已通过，等待 owner 最终逐题验收。" if audited else "三方独立审计尚未完成。"
    (temporary / "DATA_CARD.md").write_text(
        "# Gaokao-Bench 2026 全国卷 I 数学（验收候选）\n\n"
        "- 范围：2026 年全国卷 I 数学全套 19 题，150 分。\n"
        "- 确定性自动评分：选择/填空 14 题，73 分。\n"
        "- 解答题：5 题，77 分，已收录题面、参考答案和解答要点，暂不进入自动评分分母。\n"
        "- 图形依赖：客观题 0 题；解答题第 15 题需要随附立体几何图。\n"
        "- 来源：题干由 %s 交叉核对；答案由 %s 的“题干+答案”来源闭环核验；%s 仅作答案补充；%d 个已证实答案错误的来源被逐题拒绝，不进入答案门禁。\n"
        "- 署名：完整来源清单与第三方声明见 `THIRD_PARTY_NOTICES.md`、`SOURCE_REGISTER.md` 和来源 JSONL。\n"
        "- 许可：整个数据包采用 CC BY 4.0，署名 Gaokao-Bench 与准确数据版本即可再分发和改编。\n"
        f"- 状态：`acceptance_candidate`，{audit_summary}未发布。\n"
        % (
            "、".join(stem_publishers),
            "、".join(joint_publishers),
            "、".join(answer_only_publishers) if answer_only_publishers else "无",
            rejected_source_count,
        ),
        encoding="utf-8",
    )
    (temporary / "LICENSE_AND_RIGHTS.md").write_text(
        "# License and rights\n\n"
        "The complete data package, including question text, answers, crops, selection, "
        "schema and scoring metadata, is licensed under **CC BY 4.0**. Attribution to "
        "Gaokao-Bench and the exact dataset version is required. The harness and code "
        "are licensed separately under Apache-2.0. License text: "
        "https://creativecommons.org/licenses/by/4.0/. The complete CC BY 4.0 text "
        "is included in `LICENSE-DATA`.\n\n"
        "Source attribution and retrieval URLs are recorded in "
        "`THIRD_PARTY_NOTICES.md`, `SOURCE_REGISTER.md`, `sources.jsonl` and "
        "`answer_sources.jsonl`.\n",
        encoding="utf-8",
    )
    (temporary / "SOURCE_REGISTER.md").write_text(
        "# Source register\n\n"
        "| ID | Publisher | Evidence scope | Independence group | URL |\n"
        "|---|---|---|---|---|\n"
        + "\n".join(
            "| `%s` | %s | %s | `%s` | `%s` |"
            % (
                row["id"],
                row["publisher"],
                row.get("evidence_scope", "not_classified"),
                row.get("independence_group", row["id"]),
                row["url"],
            )
            for row in answer_sources
        )
        + "\n\nThe source PDF SHA-256 is `%s`. The hard gate requires at least %d "
        "independent stem groups and %d independent sources that each contain both "
        "the matched stem and the correct answer. This candidate has at least %d stem "
        "groups and %d joint-answer groups per question. Answer-only pages and rejected "
        "wrong-answer sources do not count toward the answer gate. Per-question locators, "
        "exact excerpts and rejection reasons are in `source_audit.jsonl`.\n\n"
        "Attribution is consolidated in "
        "`THIRD_PARTY_NOTICES.md`. All package material is released under CC BY 4.0 "
        "with attribution to Gaokao-Bench and this exact dataset version.\n"
        % (
            source["sha256"],
            required_stem,
            required_joint,
            minimum_stem_groups,
            minimum_joint_groups,
        ),
        encoding="utf-8",
    )
    (temporary / "RELEASE_NOTES.md").write_text(
        "# 2026 National I mathematics acceptance candidate\n\n"
        "Full 19-question catalogue; 14 objective questions / 73 points are "
        "deterministically scored, and 5 subjective questions / 77 points are "
        "catalogued with reference answers and solution outlines. Every question has "
        "at least %d independent stem-source groups and %d independent joint "
        "stem+answer sources; the enforced gate requires %d and %d. Not published; "
        "owner acceptance remains pending.\n"
        % (minimum_stem_groups, minimum_joint_groups, required_stem, required_joint),
        encoding="utf-8",
    )
    public_reproduction_note = (
        "This is an owner-acceptance candidate built from the candidate source tree. "
        "Run the commands below from that accepted source tree. Public clone/checkout "
        "is a release gate after owner approval, push, and a public rebuild; do not "
        "describe this candidate as publicly reproducible before then."
    )
    (temporary / "REPRODUCE.md").write_text(
        "# Reproduce\n\n"
        f"{public_reproduction_note}\n\n"
        "The frozen source PDF is downloaded and hash-checked by the builder when "
        "`--pdf` is omitted.\n\n"
        "Acceptance-candidate reproduction from the source tree:\n\n"
        "```bash\n"
        "cd /path/to/accepted/gaokao-bench\n"
        "git rev-parse HEAD  # must equal the revision recorded in manifest.json\n"
        "git status --porcelain  # must be empty\n"
        "python3 -m pip install -e '.[dev]'\n"
        "python3 scripts/build_2026_n1.py --spec specs/2026-math-n1.json --out data/%s\n"
        "python3 scripts/validate.py --data-dir data/%s\n"
        "python3 -m gaokao_bench.cli evaluate --data-dir data/%s --answers data/%s/examples/gold.json\n"
        "python3 -m pytest -q\n"
        "```\n\n"
        "After owner approval and publication, the public reproduction form is:\n\n"
        "```bash\n"
        "git clone %s gaokao-bench\n"
        "cd gaokao-bench\n"
        "git checkout <the revision recorded in manifest.json>\n"
        "git status --porcelain  # must be empty\n"
        "python3 -m pip install -e '.[dev]'\n"
        "python3 scripts/build_2026_n1.py --spec specs/2026-math-n1.json --out data/%s\n"
        "python3 scripts/validate.py --data-dir data/%s\n"
        "python3 -m gaokao_bench.cli evaluate --data-dir data/%s --answers data/%s/examples/gold.json\n"
        "python3 -m pytest -q\n"
        "```\n\n"
        "The builder verifies source PDF SHA-256 `%s`, re-renders all 19 crops from "
        "explicit page bounding boxes, enforces the 19/19 source gate (at least %d stem "
        "groups and %d joint stem+answer groups per question; this candidate has at "
        "least %d and %d), and "
        "writes a 73/73 objective gold result. "
        "Builder SHA-256: `%s`; spec SHA-256: `%s`; source-audit spec SHA-256: `%s`.\n"
        % (
            version,
            version,
            version,
            version,
            SOURCE_REPOSITORY,
            version,
            version,
            version,
            version,
            source["sha256"],
            required_stem,
            required_joint,
            minimum_stem_groups,
            minimum_joint_groups,
            sha256(Path(__file__).resolve()),
            sha256(spec_path),
            sha256(source_audit_spec_path),
        ),
        encoding="utf-8",
    )

    created_at = str(spec.get("created_at") or args.created_at)
    audit_counts = {
        "answer_audit_rows": len(answer_audit),
        "point_audit_rows": len(point_audit),
        "question_audit_rows": len(question_audit),
        "asset_audit_rows": len(asset_audit),
        "review_records": 0,
        "answer_evidence_sources": len(answer_sources),
        "source_audit_rows": len(source_audit_rows),
        "source_evidence_pages": len(source_pages_rows),
        "stem_dual_source_questions": source_audit_summary[
            "stem_dual_source_questions"
        ],
        "joint_answer_dual_source_questions": source_audit_summary[
            "joint_answer_dual_source_questions"
        ],
    }
    scoring_contract = {
        **public_scoring_contract(parse_benchmark_spec(spec["benchmark_spec"])),
        "profile": "strict-source-audited-points-all-or-nothing",
        "missing_or_invalid_is_zero": True,
    }
    manifest = {
        "dataset": "Gaokao-Bench",
        "dataset_version": version,
        "schema_version": SCHEMA_VERSION,
        "status": "acceptance_candidate",
        "public_release": False,
        "supersedes": None,
        "created_at": created_at,
        "content_baseline": "2026 National I Mathematics",
        "benchmark_spec": "benchmark_spec.json",
        "scope": {
            "subject": "数学",
            "year": 2026,
            "paper": "全国卷 I",
            "objective_scoring_types": ["single_choice", "multi_choice", "fill_blank"],
            "scoring_scope": "choice_and_fill_only",
            "full_paper_catalogued": True,
            "catalog_only_types": ["solve", "proof", "subjective_rubric"],
        },
        "catalog": {
            "path": "catalog/subjective_catalog.jsonl",
            "question_count": len(subjective),
            "points": subjective_points,
            "included_in_scoring": False,
            "purpose": "future_rubric_and_subjective_research_only",
        },
        "counts": {
            "papers": 1,
            "full_paper_questions": 19,
            "objective_questions": len(questions),
            "choice_questions": len([item for item in questions if item["type"] in {"single_choice", "multi_choice"}]),
            "single_choice_questions": len([item for item in questions if item["type"] == "single_choice"]),
            "multi_choice_questions": len([item for item in questions if item["type"] == "multi_choice"]),
            "fill_questions": len([item for item in questions if item["type"] == "fill_blank"]),
            "answer_slots": sum(int(item["answer_slot_count"]) for item in questions),
            "image_required_questions": objective_image_questions,
            "objective_image_required_questions": objective_image_questions,
            "catalog_subjective_image_required_questions": subjective_image_questions,
            "full_paper_image_required_questions": full_paper_image_questions,
            "catalog_subjective_questions": len(subjective),
            "catalog_subjective_points": subjective_points,
            **audit_counts,
        },
        "points": {
            "objective_total": objective_points,
            "catalog_subjective_total": subjective_points,
            "full_paper_total": 150,
            "scored_denominator": objective_points,
        },
        "source_audit": {
            "path": "source_audit.jsonl",
            "schema_version": SOURCE_AUDIT_SCHEMA_VERSION,
            "evidence_pages": "source_pages.jsonl",
            "evidence_pages_sha256": sha256(temporary / "source_pages.jsonl"),
            "evidence_page_count": len(source_pages_rows),
            "require_pinned_specs": True,
            "pinned_audit_spec": "audit/specs/source-audit.json",
            "pinned_audit_spec_sha256": sha256(source_audit_spec_path),
            "pinned_pages_spec": "audit/specs/source-pages.json",
            "pinned_pages_spec_sha256": sha256(source_pages_spec_path),
            **source_audit_summary,
        },
        "quality": {
            "quality_level": QUALITY_LEVEL,
            "review_protocol": "three_independent_audits_then_owner_signoff",
            "owner_signoff": "pending",
            "review_status": status,
            "answer_status": "three_way_audit_passed_pending_owner_signoff" if audited else "source_and_derivation_confirmed_pending_three_way_audit",
            "point_status": "three_way_audit_passed_pending_owner_signoff" if audited else "source_points_crosschecked_pending_owner_signoff",
            "question_audit_status": "audited_passed_pending_owner_signoff" if audited else "pending_independent_audit",
            "asset_status": "three_way_audit_passed_pending_owner_visual_signoff" if audited else "rendered_from_frozen_pdf_pending_visual_audit",
            "human_independent_review": "pending",
            "official_answer_source_found": False,
            "owner_review_scope": "full_paper",
            "source_pdf_sha256": source["sha256"],
            "crop_method": CROP_METHOD,
            "subjective_scoring": "catalogued_not_scored_pending_rubric",
            "source_audit_status": (
                "dual_source_gate_passed_pending_owner_signoff"
                if source_audit_summary["evidence_ready"]
                else "dual_source_gate_blocked"
            ),
            "source_audit_gate": source_audit_summary["gate"],
            "source_audit_questions": source_audit_summary["questions"],
            "stem_dual_source_questions": source_audit_summary[
                "stem_dual_source_questions"
            ],
            "joint_answer_dual_source_questions": source_audit_summary[
                "joint_answer_dual_source_questions"
            ],
            "audits": packaged_audit_reports,
        },
        "scoring": scoring_contract,
        "compatibility": {
            "dataset": version,
            "benchmark_spec": spec["benchmark_spec"]["version"],
            "engine": ENGINE_VERSION,
        },
        "versions": {
            "dataset": version,
            "benchmark_spec": spec["benchmark_spec"]["version"],
            "engine": ENGINE_VERSION,
        },
        "build_provenance": {
            "repository": SOURCE_REPOSITORY,
            "revision": revision,
            "worktree_dirty": False,
            "builder_path": "scripts/build_2026_n1.py",
            "builder_sha256": sha256(Path(__file__).resolve()),
            "spec_path": "specs/2026-math-n1.json",
            "spec_sha256": sha256(spec_path),
            "source_audit_spec_path": spec["source_audit_spec"],
            "source_audit_spec_sha256": sha256(source_audit_spec_path),
            "source_pages_spec_path": spec["source_pages_spec"],
            "source_pages_spec_sha256": sha256(source_pages_spec_path),
            "source_page_cache": str(Path(args.source_page_cache).resolve()),
            "reproduction": "REPRODUCE.md",
            "publication_gate": "owner_acceptance_then_push_and_public_rebuild",
        },
        "audit": {
            "source_audit": "source_audit.jsonl",
            "answer_sources": "answer_sources.jsonl",
            "answer_audit": "answer_audit.jsonl",
            "question_audit": "question_audit.jsonl",
            "point_audit": "point_audit.jsonl",
            "asset_audit": "asset_audit.jsonl",
            "reviews": "reviews.jsonl",
        },
        "rights": {
            "code_license": "Apache-2.0",
            "data_license": "CC-BY-4.0",
            "exam_material_license": "CC-BY-4.0",
            "public_redistribution": "allowed_with_attribution",
            "attribution_required": True,
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
        },
    }
    manifest["source_audit"]["evidence_page_count"] = len(source_pages_rows)
    content_names = [
        "benchmark_spec.json",
        "papers.jsonl",
        "questions.jsonl",
        "answer_keys.jsonl",
        "catalog/subjective_catalog.jsonl",
        "assets.jsonl",
        "coverage.csv",
        "examples/gold.json",
        "examples/empty.json",
        "examples/malformed.json",
        "examples/README.md",
        "DATA_CARD.md",
        "LICENSE_AND_RIGHTS.md",
        "LICENSE-DATA",
        "SOURCE_REGISTER.md",
        "THIRD_PARTY_NOTICES.md",
        "RELEASE_NOTES.md",
        "REPRODUCE.md",
    ] + [row["path"] for row in assets]
    audit_names = [
        "audit/specs/source-audit.json",
        "audit/specs/source-pages.json",
        "source_audit.jsonl",
        "source_pages.jsonl",
        "sources.jsonl",
        "answer_sources.jsonl",
        "answer_audit.jsonl",
        "question_audit.jsonl",
        "point_audit.jsonl",
        "asset_audit.jsonl",
        "reviews.jsonl",
    ]
    if audit_reports:
        audit_names.extend(
            sorted(
                path.relative_to(temporary).as_posix()
                for path in (temporary / "audit_reports").iterdir()
                if path.is_file()
            )
        )
    manifest["content_lineage"] = {
        "source_pdf_sha256": source["sha256"],
        "content_files": {name: sha256(temporary / name) for name in content_names},
        "audit_files": {name: sha256(temporary / name) for name in audit_names},
        "evidence_files": {
            row["path"]: row["sha256"]
            for row in source_pages_rows
        },
    }
    all_files = sorted(
        path.relative_to(temporary).as_posix()
        for path in temporary.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    )
    manifest["files"] = {
        name: {"sha256": sha256(temporary / name), "bytes": (temporary / name).stat().st_size}
        for name in all_files
    }
    write_json(temporary / "manifest.json", manifest)

    temporary.replace(out_dir)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", default=str(SPEC_PATH))
    parser.add_argument("--pdf", type=Path, help="frozen source PDF; downloads a hash-pinned copy when omitted")
    parser.add_argument("--cache-dir", default=str(ROOT / ".cache" / "gaokao-source"))
    parser.add_argument(
        "--source-page-cache",
        default=str(ROOT / ".cache" / "gaokao-source-pages" / "2026-n1"),
        help="frozen source-page evidence cache; missing files are downloaded and hash-checked",
    )
    parser.add_argument("--out", default=str(ROOT / "data" / "0.1.0-alpha.2026-n1.1"))
    parser.add_argument("--created-at", default="2026-11-12T16:40:31+08:00")
    parser.add_argument("--audit-report", action="append", default=[], help="three JSON audit reports: A, B, C")
    parser.add_argument(
        "--candidate-manifest-sha256",
        help="SHA-256 of the unaudited staging manifest that the audit reports reviewed",
    )
    parser.add_argument(
        "--allow-incomplete-source-audit",
        action="store_true",
        help="development only: build even when the dual-source gate is incomplete",
    )
    args = parser.parse_args()
    try:
        manifest = build(args)
    except (FileNotFoundError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps({
        "dataset_version": manifest["dataset_version"],
        "status": manifest["status"],
        "questions": manifest["counts"]["full_paper_questions"],
        "objective": "%d / %d" % (manifest["points"]["objective_total"], manifest["points"]["objective_total"]),
        "catalog_subjective": "%d / %d" % (manifest["counts"]["catalog_subjective_questions"], manifest["points"]["catalog_subjective_total"]),
        "review_status": manifest["quality"]["review_status"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
