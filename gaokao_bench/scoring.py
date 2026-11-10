"""Deterministic alpha scorer for Gaokao-Bench.

The public contract is intentionally narrow:

* a submission is ``{"answers": {question_id: answer, ...}}``;
* every question is all-or-nothing in the 0.0.x line;
* missing, malformed, empty, and incorrect answers receive zero;
* the weighted denominator is the sum of reviewed per-question points;
  released datasets record the review state explicitly instead of hiding it
  behind a second metric.

No model output is interpreted with an LLM.  LaTeX / Unicode math is
normalised with a small, auditable expression parser.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .benchmark_spec import (
    BenchmarkSpec,
    load_benchmark_spec,
    question_requires_vision,
    question_type,
)
from .version import (
    DATA_SCHEMA_VERSION,
    DEFAULT_DATASET_VERSION,
    SCORER_VERSION,
    SUMMARY_SCHEMA_VERSION,
)

# Default version for the checked-in alpha package.  Dataset validation uses
# the version recorded in manifest.json so signed review materialisation can
# produce a new immutable patch version without editing the scorer.
DATASET_VERSION = DEFAULT_DATASET_VERSION
SCHEMA_VERSION = DATA_SCHEMA_VERSION
ANSWER_AUDIT_STATUSES = {
    "match",
    "source_and_derivation_confirmed",
    "source_and_visual_confirmed",
    "visual_match",
    "ocr_match_pending_visual",
    "discrepancy_confirmed",
    "external_answer_error",
    "external_source_self_conflict",
    "derived_independently",
    "unverified",
    "owner_and_two_independent_reviewers_complete",
    "owner_attested_complete",
    "audited_passed",
}
SOURCE_STEM_MATCHES = {
    "exact_stem_match",
    "equivalent_stem_match",
    "variant_missing_positive_condition",
    "stem_not_visible",
}
SOURCE_ASSESSMENT_ROLES = {"supporting", "rejected"}
POINT_AUDIT_STATUSES = {
    "source_points_crosschecked_pending_owner_signoff",
    "owner_and_two_independent_reviewers_complete",
    "owner_attested_complete",
    "audited_passed",
}
REVIEW_STATES = {
    "pending",
    "complete",
    "waived_by_owner",
    "audited_passed",
    "not_required_single_owner_attestation",
}
AUDIT_PASSED_STATUSES = {"audited_passed", "passed", "complete"}
QUESTION_REVIEW_STATUSES = {
    "independently_derived_pending_owner_signoff",
    "owner_attested_complete",
    "audited_passed",
}
QUESTION_STEM_VERDICTS = {"pdf_exact", "pdf_corrected"}
QUESTION_OPTION_VERDICTS = {"pdf_exact", "pdf_corrected", "not_applicable"}
QUESTION_ASSET_VERDICTS = {"text_sufficient", "figure_required"}
QUESTION_ANSWER_VERDICTS = {"confirmed"}
PUBLIC_PACKAGE_PROFILE = "public-data"
PUBLIC_HUB_MANAGED_FILES = {".gitattributes"}
PUBLIC_REQUIRED_FILES = {
    ".gitattributes",
    "README.md",
    "CITATION.cff",
    "COMPATIBILITY.md",
    "LICENSE-DATA",
    "RELEASE_CARD.json",
    "THIRD_PARTY_NOTICES.md",
    "answer_keys.jsonl",
    "assets.jsonl",
    "brand/hero-hf-zh.png",
    "catalog/subjective_catalog.jsonl",
    "examples/README.md",
    "examples/empty.json",
    "examples/gold.json",
    "examples/malformed.json",
    "papers.jsonl",
    "questions.jsonl",
}
PUBLIC_AUDIT_REQUIRED_FILES = {
    "audit/answer_audit.jsonl",
    "audit/answer_sources.jsonl",
    "audit/asset_audit.jsonl",
    "audit/owner_signoff.json",
    "audit/point_audit.jsonl",
    "audit/question_audit.jsonl",
    "audit/README.md",
    "audit/review_protocol.json",
    "audit/reviews.jsonl",
    "audit/sources.jsonl",
    "audit/summary.json",
}
PUBLIC_IGNORED_LOCAL_PREFIXES = (".cache/", ".git/")
PUBLIC_FORBIDDEN_QUESTION_FIELDS = {"notes"}
PUBLIC_FORBIDDEN_SOURCE_FIELDS = {
    "bbox",
    "crop_method",
    "evidence_status",
    "seed_database",
    "seed_question_id",
    "seed_scan_image_path",
}
PUBLIC_FORBIDDEN_PAPER_FIELDS = {"notes"}
PUBLIC_FORBIDDEN_ASSET_FIELDS = {
    "bbox",
    "crop_method",
    "redistribution",
    "source_pdf_sha256",
}
PUBLIC_FORBIDDEN_CATALOG_FIELDS = {"source_question_id", "source_pdf_sha256"}
PUBLIC_FORBIDDEN_PATHS = {
    "DATA_CARD.md",
    "RELEASE_MATERIALIZATION.md",
    "REPRODUCE.md",
    "SOURCE_REGISTER.md",
    "answer_audit.jsonl",
    "answer_sources.jsonl",
    "asset_audit.jsonl",
    "point_audit.jsonl",
    "question_audit.jsonl",
    "reviews.jsonl",
    "sources.jsonl",
}


def _requires_canonical_metric_contract(
    dataset_version: Any, scoring: Optional[Mapping[str, Any]] = None
) -> bool:
    """Return whether a dataset opts into the single-primary-metric contract.

    ``0.0.1`` and ``0.0.3`` are immutable historical releases with a legacy
    ``main_metric`` field.  They must remain loadable for regression tests and
    citations.  New releases start at ``0.0.4`` and expose exactly one metric,
    ``scoring.primary_metric``, whose value must match the benchmark spec.
    """

    return bool(
        isinstance(scoring, Mapping)
        and scoring.get("contract") == "single_primary_metric_v1"
    )


def _validate_source_assessments(
    row: Mapping[str, Any],
    answer_sources: Mapping[str, Mapping[str, Any]],
    errors: List[str],
) -> None:
    """Validate the generic shape of question-level source assessments."""

    qid = str(row.get("question_id", ""))
    assessments = row.get("source_assessments")
    if assessments is None:
        return
    if not isinstance(assessments, list) or not assessments:
        errors.append("source assessments must be a non-empty list for %s" % qid)
        return

    seen: set[str] = set()
    for index, item in enumerate(assessments):
        if not isinstance(item, dict):
            errors.append("source assessment %d must be an object for %s" % (index, qid))
            continue
        source_id = str(item.get("source_id", ""))
        if not source_id:
            errors.append("source assessment %d has no source_id for %s" % (index, qid))
            continue
        if source_id in seen:
            errors.append("duplicate source assessment %s for %s" % (source_id, qid))
            continue
        seen.add(source_id)
        if source_id not in answer_sources:
            errors.append("source assessment references unknown source %s for %s" % (source_id, qid))
        role = item.get("role")
        if role not in SOURCE_ASSESSMENT_ROLES:
            errors.append("source assessment has invalid role for %s: %s" % (qid, role))
            continue
        supports = item.get("supports_canonical")
        if not isinstance(supports, bool):
            errors.append("source assessment supports_canonical must be boolean for %s" % qid)
            continue
        if supports != (role == "supporting"):
            errors.append("source assessment role and supports_canonical disagree for %s" % qid)
        stem_match = item.get("stem_match")
        if stem_match not in SOURCE_STEM_MATCHES:
            errors.append("source assessment has invalid stem_match for %s: %s" % (qid, stem_match))
        elif role == "supporting" and stem_match not in ("exact_stem_match", "equivalent_stem_match"):
            errors.append("supporting source does not match the question stem for %s: %s" % (qid, source_id))
        for field in ("publisher", "url", "locator", "stem_excerpt", "answer_value", "answer_excerpt", "access_method"):
            if not str(item.get(field, "")).strip():
                errors.append("source assessment is missing %s for %s" % (field, qid))
        if role == "rejected" and not str(item.get("rejection_reason") or "").strip():
            errors.append("rejected source assessment has no rejection_reason for %s" % qid)
REQUIRED_MANIFEST_FILES = {
    "answer_audit.jsonl",
    "asset_audit.jsonl",
    "answer_keys.jsonl",
    "answer_sources.jsonl",
    "assets.jsonl",
    "catalog/subjective_catalog.jsonl",
    "coverage.csv",
    "papers.jsonl",
    "point_audit.jsonl",
    "question_audit.jsonl",
    "questions.jsonl",
    "reviews.jsonl",
    "sources.jsonl",
    "examples/empty.json",
    "examples/gold.json",
    "examples/malformed.json",
    "examples/README.md",
    "DATA_CARD.md",
    "LICENSE_AND_RIGHTS.md",
    "REPRODUCE.md",
}


@dataclass(frozen=True)
class Dataset:
    data_dir: Path
    manifest: Dict[str, Any]
    papers: Tuple[Dict[str, Any], ...]
    questions: Tuple[Dict[str, Any], ...]
    answer_keys: Dict[str, Dict[str, Any]]
    catalog_subjective: Tuple[Dict[str, Any], ...]
    spec: BenchmarkSpec


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSONL at %s:%d: %s" % (path, line_number, exc)) from exc
            if not isinstance(row, dict):
                raise ValueError("JSONL row must be an object at %s:%d" % (path, line_number))
            rows.append(row)
    return rows


def load_dataset(data_dir: Path | str, validate: bool = True) -> Dataset:
    root = Path(data_dir).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    spec = load_benchmark_spec(root, manifest)
    papers = tuple(_read_jsonl(root / "papers.jsonl"))
    questions = tuple(_read_jsonl(root / "questions.jsonl"))
    answer_keys_list = _read_jsonl(root / "answer_keys.jsonl")
    answer_keys = {row["question_id"]: row for row in answer_keys_list}
    catalog_path = root / "catalog" / "subjective_catalog.jsonl"
    catalog_subjective = tuple(_read_jsonl(catalog_path) if catalog_path.is_file() else ())
    dataset = Dataset(root, manifest, papers, questions, answer_keys, catalog_subjective, spec)
    if validate:
        validate_dataset(dataset)
    return dataset


def _validate_public_manifest(dataset: Dataset) -> None:
    errors: List[str] = []
    manifest = dataset.manifest

    def nested_keys(value: Any, target: str) -> List[str]:
        found: List[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) == target:
                    found.append(str(key))
                found.extend(nested_keys(item, target))
        elif isinstance(value, list):
            for item in value:
                found.extend(nested_keys(item, target))
        return found

    if manifest.get("package_profile") != PUBLIC_PACKAGE_PROFILE:
        errors.append("public package_profile must be %s" % PUBLIC_PACKAGE_PROFILE)
    if manifest.get("public_release") is not True:
        errors.append("public dataset must set public_release=true")
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    if str(rights.get("public_redistribution", "")).startswith("not_cleared"):
        errors.append("public dataset still declares that redistribution is not cleared")
    scoring = manifest.get("scoring") if isinstance(manifest.get("scoring"), dict) else {}
    if scoring.get("primary_metric") != dataset.spec.scoring.primary_metric:
        errors.append(
            "public primary metric differs from benchmark spec: %s != %s"
            % (scoring.get("primary_metric"), dataset.spec.scoring.primary_metric)
        )
    if "main_metric" in scoring:
        errors.append("public manifest must not expose a second main_metric")
    if nested_keys(manifest, "main_metric"):
        errors.append("public manifest must expose exactly one canonical metric named scoring.primary_metric")
    if scoring.get("unit") != "ratio":
        errors.append("public primary metric unit must be ratio")
    if not str(scoring.get("definition", "")).strip():
        errors.append("public primary metric must declare its definition")
    diagnostic_metrics = scoring.get("diagnostic_metrics")
    if diagnostic_metrics != list(dataset.spec.scoring.diagnostic_metrics):
        errors.append("public diagnostic metrics differ from benchmark spec")
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    leaderboard = manifest.get("leaderboard") if isinstance(manifest.get("leaderboard"), dict) else {}
    expected_class = dataset.spec.reporting.leaderboard_class
    for name, value in (
        ("release.leaderboard_class", release.get("leaderboard_class")),
        ("leaderboard.class", leaderboard.get("class")),
    ):
        if value != expected_class:
            errors.append("public %s differs from benchmark spec" % name)
    for name, value in (
        ("release.freshness_claim_allowed", release.get("freshness_claim_allowed")),
        ("leaderboard.freshness_claim_allowed", leaderboard.get("freshness_claim_allowed")),
    ):
        if value is not dataset.spec.reporting.freshness_claim_allowed:
            errors.append("public %s differs from benchmark spec" % name)
    if release.get("separate_live_leaderboard_required") is not True:
        errors.append("public release must require a separate post-cutoff/live leaderboard")
    if leaderboard.get("post_cutoff_live_track_required") is not True:
        errors.append("public leaderboard must require a separate post-cutoff/live track")
    tracks = leaderboard.get("tracks") if isinstance(leaderboard.get("tracks"), list) else []
    if not any(
        isinstance(track, dict)
        and track.get("id") == expected_class
        and track.get("status") == "active"
        for track in tracks
    ):
        errors.append("public leaderboard is missing its active retrospective track")
    if dataset.spec.reporting.live_track_required and not any(
        isinstance(track, dict)
        and track.get("id") == "post_cutoff_live"
        and track.get("status") in {"required", "not_released"}
        for track in tracks
    ):
        errors.append("public leaderboard is missing the separate post-cutoff/live track")
    compatibility = manifest.get("compatibility") if isinstance(manifest.get("compatibility"), dict) else {}
    if compatibility.get("dataset") not in (None, dataset.manifest.get("dataset_version")):
        errors.append("public compatibility dataset version is stale")
    if compatibility.get("benchmark_spec") not in (None, dataset.spec.version):
        errors.append("public compatibility benchmark spec version is stale")
    versions = manifest.get("versions") if isinstance(manifest.get("versions"), dict) else {}
    if versions and versions != compatibility:
        errors.append("public versions and compatibility blocks differ")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        errors.append("public manifest files must be a non-empty object")
        files = {}
    required_files = set(PUBLIC_REQUIRED_FILES)
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    audit_block = manifest.get("audit")
    if isinstance(audit_block, dict):
        required_files.update(PUBLIC_AUDIT_REQUIRED_FILES)
    if int(counts.get("catalog_subjective_questions", 0)) == 0:
        required_files.discard("catalog/subjective_catalog.jsonl")
    missing_files = sorted(required_files - set(files))
    if missing_files:
        errors.append("public manifest does not cover required files: %s" % ", ".join(missing_files))
    declared_spec = manifest.get("benchmark_spec")
    if declared_spec is not None:
        if not isinstance(declared_spec, str) or not declared_spec.strip():
            errors.append("public manifest benchmark_spec must be a relative file path")
        elif declared_spec not in files:
            errors.append("public manifest does not cover benchmark_spec: %s" % declared_spec)
    elif dataset.manifest.get("dataset_version") != "0.0.1":
        errors.append("new public data versions must ship benchmark_spec.json")
    forbidden: List[str] = []
    for relative_path in files:
        if relative_path in PUBLIC_FORBIDDEN_PATHS or relative_path.startswith(
            ("docs/", "reports/", "review_packets/", "runs/", "release/", "review_ui/", "scripts/", "tests/")
        ):
            forbidden.append(relative_path)
    if forbidden:
        errors.append("public manifest contains internal paths: %s" % ", ".join(sorted(forbidden)[:5]))

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    if isinstance(audit_block, dict):
        for field in (
            "owner_signoff",
            "human_independent_review",
            "independent_review",
            "review_status",
            "question_audit_status",
            "point_status",
            "asset_status",
        ):
            value = quality.get(field)
            if isinstance(value, str) and any(
                marker in value.lower() for marker in ("pending", "waived", "provisional", "tbd", "todo")
            ):
                errors.append("public quality field %s is not audited-passed: %s" % (field, value))
        if quality.get("owner_signoff") not in {"audited_passed", "complete", "passed"}:
            errors.append("public owner_signoff must be audited_passed")
        if quality.get("review_status") not in {"audited_passed", "complete", "passed"}:
            errors.append("public review_status must be audited_passed")
        if quality.get("review_protocol") != "owner_attested_single_review":
            errors.append("public review_protocol must be owner_attested_single_review")
        rows = audit_block.get("rows") if isinstance(audit_block.get("rows"), dict) else {}
        for field, expected in (
            ("answer_audit", int(counts.get("answer_audit_rows", 0))),
            ("question_audit", int(counts.get("question_audit_rows", 0))),
            ("point_audit", int(counts.get("point_audit_rows", 0))),
            ("asset_audit", int(counts.get("asset_audit_rows", 0))),
            ("reviews", int(counts.get("review_records", 0))),
        ):
            if expected and int(rows.get(field, -1)) != expected:
                errors.append("public audit row count %s does not match manifest" % field)
        if audit_block.get("status") not in AUDIT_PASSED_STATUSES:
            errors.append("public audit evidence status must be audited-passed")
        if audit_block.get("owner_signoff") not in {"audited_passed", "complete", "passed"}:
            errors.append("public audit owner_signoff must be audited_passed")
    review_evidence = manifest.get("review_evidence") if isinstance(manifest.get("review_evidence"), dict) else {}
    if review_evidence.get("status") not in AUDIT_PASSED_STATUSES:
        errors.append("public review evidence status must be audited-passed")
    if review_evidence.get("owner_signoff") not in {"audited_passed", "complete", "passed"}:
        errors.append("public review evidence owner_signoff must be audited_passed")

    release_card_path = dataset.data_dir / "RELEASE_CARD.json"
    release_card = json.loads(release_card_path.read_text(encoding="utf-8"))
    card_scoring = release_card.get("scoring") if isinstance(release_card.get("scoring"), dict) else {}
    if card_scoring.get("primary_metric") != dataset.spec.scoring.primary_metric:
        errors.append("RELEASE_CARD primary metric differs from benchmark spec")
    if "main_metric" in card_scoring or nested_keys(release_card, "main_metric"):
        errors.append("RELEASE_CARD must not expose a second main metric")
    card_reporting = release_card.get("reporting") if isinstance(release_card.get("reporting"), dict) else {}
    if card_reporting.get("leaderboard_class") != expected_class:
        errors.append("RELEASE_CARD leaderboard class differs from benchmark spec")
    if card_reporting.get("freshness_claim_allowed") is not dataset.spec.reporting.freshness_claim_allowed:
        errors.append("RELEASE_CARD freshness policy differs from benchmark spec")
    card_audit = release_card.get("audit") if isinstance(release_card.get("audit"), dict) else {}
    if card_audit.get("status") not in AUDIT_PASSED_STATUSES:
        errors.append("RELEASE_CARD audit status must be audited-passed")
    if card_audit.get("owner_signoff") not in {"audited_passed", "complete", "passed"}:
        errors.append("RELEASE_CARD owner_signoff must be audited_passed")
    if release_card.get("compatibility") != compatibility:
        errors.append("RELEASE_CARD compatibility differs from manifest")

    actual_files = {
        path.relative_to(dataset.data_dir).as_posix()
        for path in dataset.data_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    unlisted_files = sorted(
        relative
        for relative in actual_files - set(files)
        if not relative.startswith(PUBLIC_IGNORED_LOCAL_PREFIXES)
    )
    if unlisted_files:
        errors.append("public package contains unlisted files: %s" % ", ".join(unlisted_files[:5]))

    for relative_path, metadata in sorted(files.items()):
        path_parts = Path(relative_path).parts
        if Path(relative_path).is_absolute() or ".." in path_parts:
            errors.append("unsafe public manifest path: %s" % relative_path)
            continue
        path = dataset.data_dir / relative_path
        if not path.is_file():
            errors.append("public manifest file is missing: %s" % relative_path)
            continue
        if not isinstance(metadata, dict):
            errors.append("invalid public manifest metadata for %s" % relative_path)
            continue
        if relative_path in PUBLIC_HUB_MANAGED_FILES:
            if metadata.get("managed_by") != "huggingface":
                errors.append("public manifest metadata is not marked as Hub-managed: %s" % relative_path)
            continue
        if metadata.get("bytes") != path.stat().st_size:
            errors.append("public manifest byte count mismatch: %s" % relative_path)
        if metadata.get("sha256") != file_sha256(path):
            errors.append("public manifest hash mismatch: %s" % relative_path)

    choice_types = {
        name for name, shape in dataset.spec.task_types.items() if shape.shape == "choice"
    }
    single_choice_types = {
        name
        for name in choice_types
        if dataset.spec.task_types[name].max_selections == 1
    }
    multi_choice_types = choice_types - single_choice_types
    slot_types = {
        name for name, shape in dataset.spec.task_types.items() if shape.shape == "slots"
    }
    expected_counts = {
        "papers": len(dataset.papers),
        "objective_questions": len(dataset.questions),
        "choice_questions": len([q for q in dataset.questions if question_type(q) in choice_types]),
        "single_choice_questions": len([q for q in dataset.questions if question_type(q) in single_choice_types]),
        "multi_choice_questions": len([q for q in dataset.questions if question_type(q) in multi_choice_types]),
        "fill_questions": len([q for q in dataset.questions if question_type(q) in slot_types]),
        "answer_slots": sum(int(q.get("answer_slot_count", 0)) for q in dataset.questions),
        "image_required_questions": len([q for q in dataset.questions if question_requires_vision(q)]),
        "catalog_subjective_questions": len(dataset.catalog_subjective),
        "catalog_subjective_points": sum(int(q["points"]) for q in dataset.catalog_subjective),
    }
    for field, expected in expected_counts.items():
        if int(counts.get(field, -1)) != expected:
            errors.append("public manifest count %s is stale: expected %d, got %s" % (field, expected, counts.get(field)))

    for question in dataset.questions:
        forbidden_fields = sorted(PUBLIC_FORBIDDEN_QUESTION_FIELDS & set(question))
        if forbidden_fields:
            errors.append(
                "public question %s exposes internal fields: %s"
                % (question["id"], ", ".join(forbidden_fields))
            )
        source = question.get("source")
        if isinstance(source, dict):
            source_fields = sorted(PUBLIC_FORBIDDEN_SOURCE_FIELDS & set(source))
            if source_fields:
                errors.append(
                    "public question %s exposes internal source fields: %s"
                    % (question["id"], ", ".join(source_fields))
                )
    for paper in dataset.papers:
        forbidden_fields = sorted(PUBLIC_FORBIDDEN_PAPER_FIELDS & set(paper))
        if forbidden_fields:
            errors.append(
                "public paper %s exposes internal fields: %s"
                % (paper["id"], ", ".join(forbidden_fields))
            )
    for asset in _read_jsonl(dataset.data_dir / "assets.jsonl"):
        forbidden_fields = sorted(PUBLIC_FORBIDDEN_ASSET_FIELDS & set(asset))
        if forbidden_fields:
            errors.append(
                "public asset %s exposes internal fields: %s"
                % (asset.get("question_id", ""), ", ".join(forbidden_fields))
            )
    for item in dataset.catalog_subjective:
        forbidden_fields = sorted(PUBLIC_FORBIDDEN_CATALOG_FIELDS & set(item))
        if forbidden_fields:
            errors.append(
                "public catalog item %s exposes internal fields: %s"
                % (item.get("id", ""), ", ".join(forbidden_fields))
            )

    if errors:
        raise ValueError("Gaokao-Bench public dataset validation failed:\n- " + "\n- ".join(errors))


def validate_dataset(dataset: Dataset) -> Dict[str, Any]:
    """Validate all hard invariants and return a compact audit summary."""

    errors: List[str] = []
    spec = dataset.spec
    expected = spec.expected
    dataset_version = dataset.manifest.get("dataset_version")
    manifest_scoring = (
        dataset.manifest.get("scoring")
        if isinstance(dataset.manifest.get("scoring"), dict)
        else {}
    )
    if _requires_canonical_metric_contract(dataset_version, manifest_scoring):
        if manifest_scoring.get("primary_metric") != spec.scoring.primary_metric:
            errors.append(
                "manifest primary metric differs from benchmark spec: %s != %s"
                % (manifest_scoring.get("primary_metric"), spec.scoring.primary_metric)
            )
        if "main_metric" in manifest_scoring:
            errors.append("manifest must not expose a second main_metric")
        if manifest_scoring.get("unit") != "ratio":
            errors.append("manifest primary metric unit must be ratio")
        content_lineage = (
            dataset.manifest.get("content_lineage")
            if isinstance(dataset.manifest.get("content_lineage"), dict)
            else {}
        )
        for group in ("content_files", "audit_files"):
            declared_hashes = content_lineage.get(group)
            if not isinstance(declared_hashes, dict) or not declared_hashes:
                continue
            for relative, declared in declared_hashes.items():
                path = dataset.data_dir / str(relative)
                if not path.is_file():
                    errors.append("content lineage file is missing: %s" % relative)
                elif file_sha256(path) != declared:
                    errors.append("content lineage hash is stale: %s" % relative)
    if not isinstance(dataset_version, str) or not dataset_version.strip():
        errors.append("manifest dataset_version must be a non-empty string")
    elif dataset_version != dataset.data_dir.name:
        errors.append(
            "manifest dataset_version must match the data directory name: %s != %s"
            % (dataset_version, dataset.data_dir.name)
        )
    if dataset.manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            "manifest schema_version: expected %s, got %s"
            % (SCHEMA_VERSION, dataset.manifest.get("schema_version"))
        )
    quality = (
        dataset.manifest.get("quality")
        if isinstance(dataset.manifest.get("quality"), dict)
        else {}
    )
    if quality.get("review_protocol") == "owner_attested_single_review":
        required_passed = (
            "owner_signoff",
            "review_status",
            "question_audit_status",
            "answer_status",
            "point_status",
            "asset_status",
        )
        for field in required_passed:
            if quality.get(field) not in {"audited_passed", "complete", "passed"}:
                errors.append("owner-attested quality field %s is not audited-passed: %s" % (field, quality.get(field)))
        for field in ("human_independent_review", "independent_review"):
            value = quality.get(field)
            if isinstance(value, str) and any(
                marker in value.lower() for marker in ("pending", "waived", "provisional", "tbd", "todo")
            ):
                errors.append("owner-attested quality field %s is not settled: %s" % (field, value))
    question_ids = [q["id"] for q in dataset.questions]
    if len(question_ids) != len(set(question_ids)):
        errors.append("question IDs are not unique")
    paper_ids = [paper.get("id") for paper in dataset.papers]
    if len(paper_ids) != len(set(paper_ids)):
        errors.append("paper IDs are not unique")
    known_paper_ids = set(paper_ids)
    for paper in dataset.papers:
        paper_id = paper.get("id")
        if not str(paper.get("title", "")).strip():
            errors.append("paper title is empty: %s" % paper_id)
        if paper.get("subject") != spec.subject:
            errors.append(
                "paper subject %r differs from benchmark spec subject %r: %s"
                % (paper.get("subject"), spec.subject, paper_id)
            )
        if paper.get("year") not in spec.years:
            errors.append("paper year is outside the benchmark spec years: %s" % paper_id)
    for question in dataset.questions:
        qid = str(question.get("id", ""))
        if not isinstance(question.get("points"), int) or isinstance(question.get("points"), bool) or question["points"] <= 0:
            errors.append("question points must be a positive integer: %s" % qid)
        if question.get("paper_id") not in known_paper_ids:
            errors.append("question references an unknown paper: %s" % qid)
        if question_type(question) not in spec.task_types:
            errors.append(
                "question type %r is not declared by benchmark spec: %s"
                % (question_type(question), qid)
            )
        if not str(question.get("stem", "")).strip():
            errors.append("question stem is empty: %s" % qid)

    if expected.question_count is not None and len(dataset.questions) != expected.question_count:
        errors.append("expected %d questions, got %d" % (expected.question_count, len(dataset.questions)))
    if expected.paper_count is not None and len(dataset.papers) != expected.paper_count:
        errors.append("expected %d papers, got %d" % (expected.paper_count, len(dataset.papers)))
    if expected.full_paper_question_count is not None:
        full_question_count = sum(int(paper.get("full_question_count", 0)) for paper in dataset.papers)
        if full_question_count != expected.full_paper_question_count:
            errors.append(
                "full question count must close at %d, got %d"
                % (expected.full_paper_question_count, full_question_count)
            )
    if expected.full_paper_points is not None:
        full_paper_points = sum(int(paper.get("full_paper_points", 0)) for paper in dataset.papers)
        if full_paper_points != expected.full_paper_points:
            errors.append(
                "full paper points must close at %d, got %d"
                % (expected.full_paper_points, full_paper_points)
            )

    choice_types = {name for name, shape in spec.task_types.items() if shape.shape == "choice"}
    slot_types = {name for name, shape in spec.task_types.items() if shape.shape == "slots"}
    multi_choice_types = {
        name
        for name in choice_types
        if spec.task_types[name].max_selections != 1
    }
    choices = [q for q in dataset.questions if question_type(q) in choice_types]
    fills = [q for q in dataset.questions if question_type(q) in slot_types]
    for declared_type, count in expected.task_type_counts.items():
        actual = len([q for q in dataset.questions if question_type(q) == declared_type])
        if actual != count:
            errors.append("expected %d %s questions, got %d" % (count, declared_type, actual))

    total_points = sum(int(q["points"]) for q in dataset.questions)
    if expected.total_points is not None and total_points != expected.total_points:
        errors.append("expected %d objective points, got %d" % (expected.total_points, total_points))

    paper_points: Dict[str, int] = {}
    for paper in dataset.papers:
        paper_id = paper["id"]
        selected = [q for q in dataset.questions if q["paper_id"] == paper_id]
        points = sum(int(q["points"]) for q in selected)
        paper_points[paper_id] = points
        if expected.paper_points and expected.paper_points.get(paper_id) != points:
            errors.append(
                "paper %s objective points: expected %s, got %s"
                % (paper_id, expected.paper_points.get(paper_id), points)
            )
        if paper.get("objective_points") is not None and int(paper["objective_points"]) != points:
            errors.append("paper %s objective_points field is stale" % paper_id)
        if paper.get("objective_questions") is not None and int(paper["objective_questions"]) != len(selected):
            errors.append("paper %s objective_questions field is stale" % paper_id)

    if expected.paper_points and set(paper_points) != set(expected.paper_points):
        errors.append("paper IDs do not match expected IDs")

    catalog_subjective_points = sum(int(q["points"]) for q in dataset.catalog_subjective)
    if (
        expected.catalog_question_count is not None
        and len(dataset.catalog_subjective) != expected.catalog_question_count
    ):
        errors.append(
            "expected %d catalogued subjective questions, got %d"
            % (expected.catalog_question_count, len(dataset.catalog_subjective))
        )
    if expected.catalog_points is not None and catalog_subjective_points != expected.catalog_points:
        errors.append(
            "expected %d catalogued subjective points, got %d"
            % (expected.catalog_points, catalog_subjective_points)
        )
    catalog_ids = {row.get("id") for row in dataset.catalog_subjective}
    if len(catalog_ids) != len(dataset.catalog_subjective):
        errors.append("catalogued subjective IDs are not unique")
    if any(row.get("included_in_scoring") is not False for row in dataset.catalog_subjective):
        errors.append("catalogued subjective items must be excluded from scoring")
    if catalog_ids & set(question_ids):
        errors.append("catalogued subjective IDs must not overlap scored questions")
    for paper in dataset.papers:
        paper_id = paper["id"]
        catalog_rows = [row for row in dataset.catalog_subjective if row.get("paper_id") == paper_id]
        catalog_points = sum(int(row["points"]) for row in catalog_rows)
        if int(paper.get("catalog_subjective_questions", 0)) != len(catalog_rows):
            errors.append("paper %s catalog_subjective_questions field is stale" % paper_id)
        if int(paper.get("catalog_subjective_points", 0)) != catalog_points:
            errors.append("paper %s catalog_subjective_points field is stale" % paper_id)
        if paper.get("full_paper_points") is not None and paper_points[paper_id] + catalog_points != int(paper["full_paper_points"]):
            errors.append("paper %s scored plus catalogued points do not close" % paper_id)

    for question in dataset.questions:
        qid = question["id"]
        key = dataset.answer_keys.get(qid)
        if key is None:
            errors.append("missing answer key for %s" % qid)
            continue
        shape_spec = spec.answer_shape(question_type(question))
        if int(key["points"]) != int(question["points"]):
            errors.append("answer-key points mismatch for %s" % qid)
        if key["kind"] != question_type(question):
            errors.append("answer-key kind mismatch for %s" % qid)
        if shape_spec is None:
            errors.append("answer key uses an undeclared question type for %s" % qid)
            continue
        if shape_spec.shape == "choice":
            options = question.get("options", [])
            labels = [str(option.get("label")) for option in options]
            if labels != list(spec.choice_labels):
                errors.append(
                    "choice options must be %s in order: %s"
                    % ("-".join(spec.choice_labels), qid)
                )
            if any(not str(option.get("text", "")).strip() for option in options):
                errors.append("choice option text is empty: %s" % qid)
            correct_options = key.get("correct_options", [])
            if shape_spec.min_selections is not None and len(correct_options) < shape_spec.min_selections:
                errors.append(
                    "%s key must contain at least %d options: %s"
                    % (question_type(question), shape_spec.min_selections, qid)
                )
            if shape_spec.max_selections is not None and len(correct_options) > shape_spec.max_selections:
                errors.append(
                    "%s key must contain at most %d options: %s"
                    % (question_type(question), shape_spec.max_selections, qid)
                )
            if any(label not in spec.choice_labels for label in correct_options):
                errors.append("invalid option label for %s" % qid)
            if key.get("canonical") != "".join(sorted(correct_options)):
                errors.append("choice canonical does not match sorted options for %s" % qid)
        elif shape_spec.shape == "slots":
            slots = key.get("slots", [])
            if not slots:
                errors.append("fill answer key has no slots: %s" % qid)
            if int(question.get("answer_slot_count") or 0) != len(slots):
                errors.append("answer slot count mismatch for %s" % qid)
            for slot in slots:
                matcher = slot.get("matcher")
                validator = matcher.get("validator") if isinstance(matcher, Mapping) else matcher
                if spec.validators and validator not in spec.validators:
                    errors.append("unknown answer validator %r for %s" % (validator, qid))
            expected_canonical = [slot.get("canonical") for slot in slots]
            if len(expected_canonical) == 1:
                expected_canonical = expected_canonical[0]
            if key.get("canonical") != expected_canonical:
                errors.append("fill canonical does not match answer slots for %s" % qid)
        elif shape_spec.shape == "rubric":
            errors.append("rubric-scored questions are not executable by engine 0.1: %s" % qid)
        custom = key.get("custom_validator")
        if custom and spec.custom_validators and custom not in spec.custom_validators:
            errors.append("unknown custom validator %r for %s" % (custom, qid))
        image_path = question.get("image_path")
        if image_path:
            asset = dataset.data_dir / image_path
            if not asset.is_file():
                errors.append("missing image asset for %s: %s" % (qid, image_path))
            elif question.get("image_sha256"):
                if file_sha256(asset) != question["image_sha256"]:
                    errors.append("image hash mismatch for %s" % qid)
        elif question_requires_vision(question):
            errors.append("required-image question has no image_path: %s" % qid)

    for key_id in dataset.answer_keys:
        if key_id not in set(question_ids):
            errors.append("orphan answer key: %s" % key_id)

    required_assets = [q for q in dataset.questions if question_requires_vision(q)]
    required_asset_ids = {q["id"] for q in required_assets}
    if expected.required_image_ids is not None and required_asset_ids != set(expected.required_image_ids):
        errors.append(
            "image-required question set differs: expected %s, got %s"
            % (sorted(expected.required_image_ids), sorted(required_asset_ids))
        )
    if dataset.manifest.get("package_profile") == PUBLIC_PACKAGE_PROFILE:
        if errors:
            raise ValueError("Gaokao-Bench dataset validation failed:\n- " + "\n- ".join(errors))
        _validate_public_manifest(dataset)
        return {
            "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
            "benchmark_spec_id": spec.id,
            "benchmark_spec_version": spec.version,
            "benchmark_spec_sha256": spec.fingerprint(),
            "papers": len(dataset.papers),
            "questions": len(dataset.questions),
            "choice_questions": len(choices),
            "fill_questions": len(fills),
            "multi_choice_questions": len(
                [q for q in choices if question_type(q) in multi_choice_types]
            ),
            "answer_slots": sum(int(q.get("answer_slot_count") or 0) for q in dataset.questions),
            "objective_points": total_points,
            "paper_points": paper_points,
            "scored_scope": "deterministic_objective",
            "catalog_subjective_questions": len(dataset.catalog_subjective),
            "catalog_subjective_points": catalog_subjective_points,
            "image_required_questions": len(required_assets),
        "question_audit_rows": int(
            (dataset.manifest.get("counts") or {}).get("question_audit_rows", 0)
        ),
        }

    questions_by_id = {question["id"]: question for question in dataset.questions}
    answer_sources = {
        str(row.get("id")): row
        for row in _read_jsonl(dataset.data_dir / "answer_sources.jsonl")
        if row.get("id")
    }
    answer_audit = _read_jsonl(dataset.data_dir / "answer_audit.jsonl")
    if len(answer_audit) != len(dataset.questions):
        errors.append(
            "answer audit covers %d/%d questions" % (len(answer_audit), len(dataset.questions))
        )
    answer_audit_ids: set[str] = set()
    for row in answer_audit:
        qid = str(row.get("question_id", ""))
        if qid in answer_audit_ids:
            errors.append("duplicate answer audit row: %s" % qid)
            continue
        answer_audit_ids.add(qid)
        question = questions_by_id.get(qid)
        key = dataset.answer_keys.get(qid)
        if question is None or key is None:
            errors.append("answer audit contains unknown question: %s" % qid)
            continue
        if row.get("paper_id") != question.get("paper_id"):
            errors.append("answer audit paper mismatch for %s" % qid)
        if row.get("question_type") != question_type(question):
            errors.append("answer audit question type mismatch for %s" % qid)
        if row.get("current_canonical") != key.get("canonical"):
            errors.append("answer audit current canonical differs from answer key for %s" % qid)
        if row.get("reviewed_canonical") != key.get("canonical"):
            errors.append("answer audit reviewed canonical differs from answer key for %s" % qid)
        if row.get("status") not in ANSWER_AUDIT_STATUSES:
            errors.append("answer audit has unknown status for %s: %s" % (qid, row.get("status")))
        if row.get("human_independent_review") not in REVIEW_STATES:
            errors.append("answer audit has invalid independent-review state for %s" % qid)
        if not str(row.get("decision", "")).strip():
            errors.append("answer audit has no decision for %s" % qid)
        if not str(row.get("external_locator", "")).strip():
            errors.append("answer audit has no external locator for %s" % qid)
        if not str(row.get("external_excerpt", "")).strip():
            errors.append("answer audit has no external evidence excerpt for %s" % qid)
        source_ids = [
            row.get("external_source_id"),
            row.get("secondary_source_id"),
            *(row.get("supporting_source_ids") or []),
            *(row.get("rejected_source_ids") or []),
        ]
        for source_id in source_ids:
            if source_id is not None and source_id not in answer_sources:
                errors.append("answer audit references unknown source %s for %s" % (source_id, qid))
        _validate_source_assessments(row, answer_sources, errors)
        if spec.raw.get("audit_profile") == "legacy_2025_math":
            from .specs.legacy_2025_math_audit import validate_q13_gate

            validate_q13_gate(row, errors)

    source_ids = {str(row.get("id")) for row in _read_jsonl(dataset.data_dir / "sources.jsonl")}
    source_ids.update(answer_sources)
    point_audit = _read_jsonl(dataset.data_dir / "point_audit.jsonl")
    if len(point_audit) != len(dataset.questions):
        errors.append(
            "point audit covers %d/%d questions" % (len(point_audit), len(dataset.questions))
        )
    point_audit_ids: set[str] = set()
    for row in point_audit:
        qid = str(row.get("question_id", ""))
        if qid in point_audit_ids:
            errors.append("duplicate point audit row: %s" % qid)
            continue
        point_audit_ids.add(qid)
        question = questions_by_id.get(qid)
        if question is None:
            errors.append("point audit contains unknown question: %s" % qid)
            continue
        if row.get("item_id") != qid:
            errors.append("point audit item_id differs from question_id for %s" % qid)
        if row.get("paper_id") != question.get("paper_id"):
            errors.append("point audit paper mismatch for %s" % qid)
        if row.get("kind") != question_type(question):
            errors.append("point audit kind mismatch for %s" % qid)
        if row.get("current_points") != question.get("points") or row.get("reviewed_points") != question.get("points"):
            errors.append("point audit does not match question points for %s" % qid)
        if row.get("delta") != 0:
            errors.append("point audit current delta must be zero for %s" % qid)
        if row.get("status") not in POINT_AUDIT_STATUSES:
            errors.append("point audit has unknown status for %s: %s" % (qid, row.get("status")))
        if row.get("human_independent_review") not in REVIEW_STATES:
            errors.append("point audit has invalid independent-review state for %s" % qid)
        for source_id in row.get("source_ids") or []:
            if source_id not in source_ids:
                errors.append("point audit references unknown source %s for %s" % (source_id, qid))

    question_audit_path = dataset.data_dir / "question_audit.jsonl"
    if not question_audit_path.is_file():
        errors.append("question_audit.jsonl is missing")
    else:
        question_audit = _read_jsonl(question_audit_path)
        audit_ids = [str(row.get("question_id", "")) for row in question_audit]
        if len(question_audit) != len(dataset.questions):
            errors.append(
                "expected %d question audit rows, got %d"
                % (len(dataset.questions), len(question_audit))
            )
        if len(set(audit_ids)) != len(audit_ids):
            errors.append("question audit IDs are not unique")
        if set(audit_ids) != set(question_ids):
            errors.append("question audit coverage does not match scored questions")
        for row in question_audit:
            qid = str(row.get("question_id", ""))
            question = questions_by_id.get(qid)
            if question is None:
                continue
            if row.get("paper_id") != question.get("paper_id") or row.get("number") != question.get("number"):
                errors.append("question audit identity mismatch for %s" % qid)
            if row.get("stem_verdict") not in QUESTION_STEM_VERDICTS:
                errors.append("question audit has invalid stem verdict for %s" % qid)
            if row.get("option_verdict") not in QUESTION_OPTION_VERDICTS:
                errors.append("question audit has invalid option verdict for %s" % qid)
            if row.get("asset_verdict") not in QUESTION_ASSET_VERDICTS:
                errors.append("question audit has invalid asset verdict for %s" % qid)
            if row.get("asset_verdict") != (
                "figure_required" if question.get("asset_dependency") == "required" else "text_sufficient"
            ):
                errors.append("question audit asset verdict differs from dependency for %s" % qid)
            if row.get("answer_verdict") not in QUESTION_ANSWER_VERDICTS:
                errors.append("question audit has invalid answer verdict for %s" % qid)
            if row.get("answer_canonical") != dataset.answer_keys[qid].get("canonical"):
                errors.append("question audit answer canonical differs from answer key for %s" % qid)
            if not str(row.get("derivation", "")).strip():
                errors.append("question audit has no derivation for %s" % qid)
            if not str(row.get("reviewer", "")).strip() or not str(row.get("reviewed_at", "")).strip():
                errors.append("question audit has no reviewer or timestamp for %s" % qid)
            if row.get("review_status") not in QUESTION_REVIEW_STATUSES:
                errors.append("question audit has unexpected review status for %s" % qid)
            source = question.get("source") or {}
            if row.get("source_pdf_sha256") != source.get("source_pdf_sha256"):
                errors.append("question audit source PDF hash differs for %s" % qid)
            if row.get("page_number") != source.get("page_number"):
                errors.append("question audit page differs for %s" % qid)
            if row.get("bbox") != source.get("bbox"):
                errors.append("question audit bbox differs for %s" % qid)

    counts = dataset.manifest.get("counts") if isinstance(dataset.manifest.get("counts"), dict) else {}
    expected_counts = {
        "objective_questions": len(dataset.questions),
        "answer_audit_rows": len(answer_audit),
        "point_audit_rows": len(point_audit),
        "question_audit_rows": len(question_audit) if question_audit_path.is_file() else -1,
    }
    for field, expected_count in expected_counts.items():
        if int(counts.get(field, -1)) != expected_count:
            errors.append(
                "manifest count %s is stale: expected %d, got %s"
                % (field, expected_count, counts.get(field))
            )

    quality = dataset.manifest.get("quality") if isinstance(dataset.manifest.get("quality"), dict) else {}
    review_lineage = dataset.manifest.get("review_lineage")
    if quality.get("review_protocol") == "owner_attested_single_review":
        signoff_path = dataset.data_dir / "owner_signoff.json"
        if not signoff_path.is_file():
            errors.append("owner_signoff.json is missing")
        else:
            signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
            if signoff.get("status") not in {"audited_passed", "complete", "passed"}:
                errors.append("owner_signoff status is not audited-passed")
            if signoff.get("dataset_version") != dataset_version:
                errors.append("owner_signoff is bound to a different dataset version")
            if signoff.get("decision") != "accept_all" or signoff.get("owner_attested") is not True:
                errors.append("owner_signoff does not record an accept-all owner attestation")
            for name, digest in (signoff.get("input_sha256") or {}).items():
                path = dataset.data_dir / str(name)
                if not path.is_file():
                    errors.append("owner_signoff input is missing: %s" % name)
                elif digest != file_sha256(path):
                    errors.append("owner_signoff input hash is stale: %s" % name)
        review_rows = _read_jsonl(dataset.data_dir / "reviews.jsonl")
        expected_ids = set(question_ids)
        seen_ids: set[str] = set()
        for row in review_rows:
            target_id = str(row.get("target_id", ""))
            if row.get("role") != "owner" or row.get("target_type") != "answer_and_points":
                errors.append("owner-attested review has an unexpected role or target type: %s" % target_id)
            if target_id in seen_ids:
                errors.append("duplicate owner-attested review event: %s" % target_id)
            seen_ids.add(target_id)
            if row.get("decision") != "accept":
                errors.append("owner-attested review is not accepted for %s" % target_id)
            if row.get("value") != row.get("canonical_value"):
                errors.append("owner-attested review value differs from canonical for %s" % target_id)
            if not str(row.get("reviewed_at", "")).strip():
                errors.append("owner-attested review has no timestamp for %s" % target_id)
        if seen_ids != expected_ids:
            errors.append(
                "owner-attested review coverage differs from scored questions: %d/%d"
                % (len(seen_ids), len(expected_ids))
            )
        if int(counts.get("review_records", -1)) != len(review_rows):
            errors.append("manifest review_records does not match owner-attested reviews.jsonl")
    if quality.get("review_materialization") == "complete" or review_lineage is not None:
        if not isinstance(review_lineage, dict):
            errors.append("materialized dataset is missing review_lineage")
        else:
            if review_lineage.get("source_dataset_version") == dataset.manifest.get("dataset_version"):
                errors.append("review_lineage source version must differ from the materialized version")
            materialization_document = review_lineage.get("materialization_document")
            if not isinstance(materialization_document, str) or not (dataset.data_dir / materialization_document).is_file():
                errors.append("review_lineage materialization document is missing")
        review_rows = _read_jsonl(dataset.data_dir / "reviews.jsonl")
        expected_reviews = {
            (role, target_type, qid)
            for role in ("owner", "reviewer_1", "reviewer_2")
            for target_type in ("answer", "points")
            for qid in question_ids
        }
        seen_reviews = set()
        for row in review_rows:
            key = (row.get("role"), row.get("target_type"), row.get("target_id"))
            if key in seen_reviews:
                errors.append("duplicate review event: %s" % (key,))
            seen_reviews.add(key)
            if row.get("decision") not in ("accept", "revise", "reject"):
                errors.append("invalid review decision for %s: %s" % (key, row.get("decision")))
            if row.get("decision") != "accept":
                errors.append("materialised review still contains an unresolved decision for %s" % (key,))
            if row.get("value") != row.get("canonical_value"):
                errors.append("materialised review value differs from canonical for %s" % (key,))
            if not isinstance(row.get("review_event_id"), str) or not row["review_event_id"]:
                errors.append("review event has no review_event_id: %s" % (key,))
            if not _is_sha256(row.get("review_report_sha256")):
                errors.append("review event has no valid report hash: %s" % (key,))
        missing_reviews = expected_reviews - seen_reviews
        extra_reviews = seen_reviews - expected_reviews
        if missing_reviews:
            errors.append("review coverage is missing %d events" % len(missing_reviews))
        if extra_reviews:
            errors.append("review coverage contains %d unexpected events" % len(extra_reviews))
        if int((dataset.manifest.get("counts") or {}).get("review_records", -1)) != len(review_rows):
            errors.append("manifest review_records does not match reviews.jsonl")
        if isinstance(review_lineage, dict) and review_lineage.get("review_event_count") != len(review_rows):
            errors.append("review_lineage event count does not match reviews.jsonl")

    manifest_files = dataset.manifest.get("files")
    if not isinstance(manifest_files, dict):
        errors.append("manifest files must be an object")
    else:
        missing_files = sorted(REQUIRED_MANIFEST_FILES - set(manifest_files))
        if missing_files:
            errors.append("manifest does not cover required files: %s" % ", ".join(missing_files))
        for relative_path, metadata in sorted(manifest_files.items()):
            path_parts = Path(relative_path).parts
            if Path(relative_path).is_absolute() or ".." in path_parts:
                errors.append("unsafe manifest path: %s" % relative_path)
                continue
            path = dataset.data_dir / relative_path
            if not path.is_file():
                errors.append("manifest file is missing: %s" % relative_path)
                continue
            if not isinstance(metadata, dict):
                errors.append("invalid manifest metadata for %s" % relative_path)
                continue
            if metadata.get("bytes") != path.stat().st_size:
                errors.append("manifest byte count mismatch: %s" % relative_path)
            if metadata.get("sha256") != file_sha256(path):
                errors.append("manifest hash mismatch: %s" % relative_path)

    required_assets = [q for q in dataset.questions if question_requires_vision(q)]
    required_asset_ids = {q["id"] for q in required_assets}
    if expected.required_image_ids is not None and required_asset_ids != set(expected.required_image_ids):
        errors.append(
            "image-required question set differs: expected %s, got %s"
            % (sorted(expected.required_image_ids), sorted(required_asset_ids))
        )

    if errors:
        raise ValueError("Gaokao-Bench dataset validation failed:\n- " + "\n- ".join(errors))

    return {
        "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
        "benchmark_spec_id": spec.id,
        "benchmark_spec_version": spec.version,
        "benchmark_spec_sha256": spec.fingerprint(),
        "papers": len(dataset.papers),
        "questions": len(dataset.questions),
        "choice_questions": len(choices),
        "fill_questions": len(fills),
        "multi_choice_questions": len(
            [q for q in choices if question_type(q) in multi_choice_types]
        ),
        "answer_slots": sum(int(q.get("answer_slot_count") or 0) for q in dataset.questions),
        "objective_points": total_points,
        "paper_points": paper_points,
        "scored_scope": "deterministic_objective",
        "catalog_subjective_questions": len(dataset.catalog_subjective),
        "catalog_subjective_points": catalog_subjective_points,
        "image_required_questions": len(required_assets),
        "question_audit_rows": len(question_audit) if question_audit_path.is_file() else 0,
    }


def file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _clean_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = text.replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("　", " ")
    return text


def _strip_answer_prefix(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r"^\s*(?:final\s+answer|answer|答案|最终答案)\s*[:：]\s*", "", text, flags=re.I)
    return text.strip()


def _normalise_math(text: str) -> str:
    text = _strip_answer_prefix(text)
    text = text.replace("$", "").replace("\\(", "").replace("\\)", "")
    text = text.replace("\\[", "").replace("\\]", "")
    text = text.replace("\\left", "").replace("\\right", "")
    text = text.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    text = re.sub(r"\\(?:times|cdot)", "*", text)
    text = text.replace("\\pi", "pi").replace("π", "pi")
    text = text.replace("\\sqrt", "sqrt")
    text = text.replace("√", "sqrt")
    text = text.replace("×", "*").replace("·", "*").replace("⋅", "*")
    text = text.replace("−", "-").replace("–", "-")
    text = text.replace("°", "").replace("度", "")
    text = text.replace("\\,", "").replace("\\;", "").replace("\\!", "")
    text = text.replace(" ", "").replace("\n", "").replace("\t", "")

    # Replace LaTeX fractions and square roots from the inside out.
    for _ in range(4):
        new_text = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"((\1)/(\2))", text)
        new_text = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"sqrt(\1)", new_text)
        if new_text == text:
            break
        text = new_text
    text = text.replace("{", "(").replace("}", ")")
    text = re.sub(r"\\sqrt\s*([0-9]+(?:\.[0-9]+)?)", r"sqrt(\1)", text)
    text = text.replace("sqrt", "sqrt")
    return text


_NUM_TOKEN_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+|pi|sqrt|[()+\-*/^])")


def _numeric_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    index = 0
    while index < len(text):
        match = _NUM_TOKEN_RE.match(text, index)
        if not match:
            raise ValueError("unsupported character in numeric answer: %r" % text[index:])
        token = match.group(0)
        tokens.append(token)
        index = match.end()
    return tokens


class _NumberParser:
    def __init__(self, tokens: Sequence[str]):
        self.tokens = list(tokens)
        self.index = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected: Optional[str] = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError("unexpected end or token in numeric answer")
        self.index += 1
        return token

    def parse(self) -> float:
        value = self.parse_expr()
        if self.peek() is not None:
            raise ValueError("trailing tokens in numeric answer")
        return value

    def parse_expr(self) -> float:
        value = self.parse_term()
        while self.peek() in ("+", "-"):
            operator = self.take()
            rhs = self.parse_term()
            value = value + rhs if operator == "+" else value - rhs
        return value

    def parse_term(self) -> float:
        value = self.parse_unary()
        while True:
            token = self.peek()
            if token in ("*", "/"):
                self.take()
                rhs = self.parse_unary()
                if token == "/":
                    if abs(rhs) < 1e-15:
                        raise ValueError("division by zero")
                    value = value / rhs
                else:
                    value = value * rhs
            elif token is not None and token not in ("+", "-", ")", "^"):
                rhs = self.parse_unary()
                value = value * rhs
            else:
                break
        return value

    def parse_unary(self) -> float:
        token = self.peek()
        if token == "+":
            self.take()
            return self.parse_unary()
        if token == "-":
            self.take()
            return -self.parse_unary()
        return self.parse_power()

    def parse_power(self) -> float:
        value = self.parse_primary()
        if self.peek() == "^":
            self.take()
            value = value ** self.parse_unary()
        return value

    def parse_primary(self) -> float:
        token = self.take()
        if token == "(":
            value = self.parse_expr()
            self.take(")")
            return value
        if token == "pi":
            return math.pi
        if token == "sqrt":
            if self.peek() == "(":
                self.take("(")
                value = self.parse_expr()
                self.take(")")
            else:
                value = self.parse_primary()
            if value < -1e-15:
                raise ValueError("negative square root")
            return math.sqrt(max(0.0, value))
        if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", token):
            return float(token)
        raise ValueError("unsupported numeric token %r" % token)


def parse_numeric_expression(value: Any) -> float:
    text = _normalise_math(str(value))
    if not text:
        raise ValueError("empty numeric answer")
    return _NumberParser(_numeric_tokens(text)).parse()


def _normalise_brackets(text: str) -> str:
    return (
        text.replace("（", "(")
        .replace("）", ")")
        .replace("［", "[")
        .replace("］", "]")
        .replace("【", "[")
        .replace("】", "]")
    )


def _normalise_interval_syntax(value: Any) -> str:
    """Normalise the small, auditable interval subset accepted by 0.1.

    This intentionally does not evaluate set-builder predicates in general.
    It only accepts a comparison chain around one variable, so malformed or
    directionally inconsistent answers cannot silently become an interval.
    """

    text = _strip_answer_prefix(value)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("$", "").replace("\\left", "").replace("\\right", "")
    text = text.replace("\\{", "{").replace("\\}", "}")
    text = text.replace("\\mid", "|").replace("\\vert", "|")
    text = text.replace("\\lvert", "|").replace("\\rvert", "|")
    text = text.replace("\\in", "∈")
    text = text.replace("\\mathbb{R}", "R").replace("\\mathbf{R}", "R")
    text = text.replace("\\leqslant", "<=").replace("\\geqslant", ">=")
    text = text.replace("\\leq", "<=").replace("\\le", "<=")
    text = text.replace("\\geq", ">=").replace("\\ge", ">=")
    text = text.replace("\\lt", "<").replace("\\gt", ">")
    text = text.replace("≤", "<=").replace("≥", ">=")
    text = text.replace("≦", "<=").replace("≧", ">=")
    text = text.replace("＜", "<").replace("＞", ">")
    text = _normalise_brackets(text)
    text = text.replace("，", ",").replace("．", ".").replace("。", "")
    return re.sub(r"\s+", "", text)


def _parse_set_builder_interval(text: str) -> Optional[Tuple[float, float, bool, bool]]:
    """Parse ``{x | a <= x <= b}`` and reject inconsistent predicates."""

    if not (text.startswith("{") and text.endswith("}")):
        return None
    body = text[1:-1]
    if "|" not in body:
        return None
    variable_text, condition = body.split("|", 1)
    variable_text = re.sub(r"∈(?:R|ℝ)$", "", variable_text)
    if not re.fullmatch(r"[A-Za-z]", variable_text):
        raise ValueError("set-builder answer must bind exactly one variable")
    membership = re.search(r",([A-Za-z])∈(?:R|ℝ)$", condition)
    if membership:
        if membership.group(1) != variable_text:
            raise ValueError("set-builder membership clause uses a different variable")
        condition = condition[: membership.start()]
    match = re.fullmatch(r"(.+?)(<=|<|>=|>)([A-Za-z])(<=|<|>=|>)(.+)", condition)
    if not match:
        raise ValueError("set-builder answer is not a comparison chain")
    lower_text, left_op, variable, right_op, upper_text = match.groups()
    if variable != variable_text:
        raise ValueError("set-builder answer uses two different variables")

    if left_op in ("<", "<=") and right_op in ("<", "<="):
        low_text, high_text = lower_text, upper_text
        low_inclusive = left_op == "<="
        high_inclusive = right_op == "<="
    elif left_op in (">", ">=") and right_op in (">", ">="):
        low_text, high_text = upper_text, lower_text
        low_inclusive = right_op == ">="
        high_inclusive = left_op == ">="
    else:
        raise ValueError("set-builder comparisons must point the same way")

    low = parse_numeric_expression(low_text)
    high = parse_numeric_expression(high_text)
    if low > high:
        raise ValueError("interval lower endpoint is greater than upper endpoint")
    return low, high, low_inclusive, high_inclusive


def parse_interval(value: Any) -> Tuple[float, float, bool, bool]:
    text = _normalise_interval_syntax(value)
    set_builder = _parse_set_builder_interval(text)
    if set_builder is not None:
        return set_builder
    text = re.sub(r"^[A-Za-z]∈", "", text)
    match = re.fullmatch(r"([\[\(])(.*),(.*)([\]\)])", text)
    if not match:
        raise ValueError("answer is not an interval")
    low = parse_numeric_expression(match.group(2))
    high = parse_numeric_expression(match.group(3))
    if low > high:
        raise ValueError("interval lower endpoint is greater than upper endpoint")
    return low, high, match.group(1) == "[", match.group(4) == "]"


_CIRCLED_DIGITS = {"①": "1", "②": "2", "③": "3", "④": "4", "⑤": "5", "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9"}


def parse_set_tokens(value: Any) -> Tuple[str, ...]:
    text = str(value)
    for symbol, digit in _CIRCLED_DIGITS.items():
        text = text.replace(symbol, digit + ",")
    text = _clean_text(text)
    text = re.sub(r"^\s*(?:correct|正确|答案为|答案)\s*[:：]?\s*", "", text, flags=re.I)
    text = re.sub(r"[()（）\[\]{}<>《》]", " ", text)
    if re.search(r"[^0-9,，、;；\s]+", text):
        raise ValueError("set answer contains unsupported characters")
    tokens = tuple(sorted(re.findall(r"\d+", text)))
    if not tokens:
        raise ValueError("set answer is empty")
    return tokens


def _normalise_vector_expression(value: Any) -> str:
    text = _strip_answer_prefix(value)
    text = text.replace("$", "")
    if "=" in text:
        text = text.rsplit("=", 1)[-1]
    text = text.replace("𝒂", "a").replace("𝒃", "b")
    text = text.replace("\\boldsymbol", "").replace("\\mathbf", "").replace("\\vec", "")
    text = re.sub(r"\\overrightarrow\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\(?:dfrac|tfrac)", r"\\frac", text)
    text = text.replace("\\cdot", "*").replace("·", "*").replace("×", "*")
    for _ in range(4):
        new_text = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"((\1)/(\2))", text)
        if new_text == text:
            break
        text = new_text
    text = text.replace("{", "").replace("}", "")
    text = text.replace(" ", "").replace("\n", "")
    return text


_VEC_TOKEN_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+|[ab()+\-*/])")


def _vector_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    index = 0
    while index < len(text):
        match = _VEC_TOKEN_RE.match(text, index)
        if not match:
            raise ValueError("unsupported character in vector answer: %r" % text[index:])
        tokens.append(match.group(0))
        index = match.end()
    return tokens


class _VectorParser:
    """Parse linear expressions in the two vectors ``a`` and ``b``."""

    def __init__(self, tokens: Sequence[str]):
        self.tokens = list(tokens)
        self.index = 0

    def peek(self) -> Optional[str]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected: Optional[str] = None) -> str:
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise ValueError("unexpected end or token in vector answer")
        self.index += 1
        return token

    def parse(self) -> Tuple[float, float]:
        value = self.parse_expr()
        if self.peek() is not None:
            raise ValueError("trailing tokens in vector answer")
        if abs(value[0]) > 1e-12:
            raise ValueError("answer contains a constant term")
        return value[1], value[2]

    def parse_expr(self) -> Tuple[float, float, float]:
        value = self.parse_term()
        while self.peek() in ("+", "-"):
            operator = self.take()
            rhs = self.parse_term()
            value = _vec_add(value, rhs) if operator == "+" else _vec_sub(value, rhs)
        return value

    def parse_term(self) -> Tuple[float, float, float]:
        value = self.parse_unary()
        while True:
            token = self.peek()
            if token in ("*", "/"):
                self.take()
                rhs = self.parse_unary()
                if token == "/":
                    if not _vec_is_scalar(rhs) or abs(rhs[0]) < 1e-15:
                        raise ValueError("can only divide a vector expression by a non-zero scalar")
                    value = _vec_scale(value, 1.0 / rhs[0])
                else:
                    value = _vec_mul(value, rhs)
            elif token is not None and token not in ("+", "-", ")"):
                value = _vec_mul(value, self.parse_unary())
            else:
                break
        return value

    def parse_unary(self) -> Tuple[float, float, float]:
        token = self.peek()
        if token == "+":
            self.take()
            return self.parse_unary()
        if token == "-":
            self.take()
            return _vec_scale(self.parse_unary(), -1.0)
        return self.parse_primary()

    def parse_primary(self) -> Tuple[float, float, float]:
        token = self.take()
        if token == "(":
            value = self.parse_expr()
            self.take(")")
            return value
        if token == "a":
            return (0.0, 1.0, 0.0)
        if token == "b":
            return (0.0, 0.0, 1.0)
        if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", token):
            return (float(token), 0.0, 0.0)
        raise ValueError("unsupported vector token %r" % token)


def _vec_add(x: Tuple[float, float, float], y: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (x[0] + y[0], x[1] + y[1], x[2] + y[2])


def _vec_sub(x: Tuple[float, float, float], y: Tuple[float, float, float]) -> Tuple[float, float, float]:
    return (x[0] - y[0], x[1] - y[1], x[2] - y[2])


def _vec_scale(x: Tuple[float, float, float], k: float) -> Tuple[float, float, float]:
    return (x[0] * k, x[1] * k, x[2] * k)


def _vec_is_scalar(x: Tuple[float, float, float]) -> bool:
    return abs(x[1]) < 1e-12 and abs(x[2]) < 1e-12


def _vec_mul(x: Tuple[float, float, float], y: Tuple[float, float, float]) -> Tuple[float, float, float]:
    if _vec_is_scalar(x):
        return _vec_scale(y, x[0])
    if _vec_is_scalar(y):
        return _vec_scale(x, y[0])
    raise ValueError("product of two non-scalar vector expressions is not supported")


def parse_vector_linear(value: Any) -> Tuple[float, float]:
    return _VectorParser(_vector_tokens(_normalise_vector_expression(value))).parse()


def parse_choice_set(value: Any, labels: Sequence[str] = ("A", "B", "C", "D")) -> Tuple[str, ...]:
    allowed = tuple(str(label) for label in labels)
    if not allowed or len(set(allowed)) != len(allowed):
        raise ValueError("choice labels must be unique and non-empty")
    allowed_set = set(allowed)
    if isinstance(value, (list, tuple, set)):
        if not value:
            raise ValueError("empty choice answer")
        items = [_clean_text(item).upper() for item in value]
        if any(item not in allowed_set for item in items):
            raise ValueError("choice list contains an invalid option")
        return tuple(sorted(set(items)))
    text = _strip_answer_prefix(value)
    text = text.replace("。", "").replace(".", "")
    separators = r"\s,，、;；()（）"
    pattern = r"^[%s%s]+$" % (re.escape("".join(allowed)), separators)
    if not text or not re.fullmatch(pattern, text.upper()):
        raise ValueError("choice answer must contain only declared option labels and separators")
    label_pattern = "[%s]" % re.escape("".join(allowed))
    labels_found = re.findall(label_pattern, text.upper())
    if not labels_found:
        raise ValueError("empty choice answer")
    return tuple(sorted(set(labels_found)))


def _split_slots(value: Any, count: int) -> List[Any]:
    if count == 1:
        if isinstance(value, (list, tuple)) and len(value) == 1:
            return [value[0]]
        return [value]
    if isinstance(value, (list, tuple)):
        if len(value) != count:
            raise ValueError("expected %d answer slots, got %d" % (count, len(value)))
        return list(value)
    text = str(value)
    parts = re.split(r"\s*(?:\|\|\||；|;)\s*", text)
    if len(parts) != count:
        raise ValueError("expected %d answer slots separated by '；', got %d" % (count, len(parts)))
    return parts


def _strip_text_answer(value: Any) -> str:
    return re.sub(r"\s+", " ", _strip_answer_prefix(value)).strip()


def _match_slot(value: Any, slot: Mapping[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    matcher_config = slot["matcher"]
    if isinstance(matcher_config, Mapping):
        matcher = str(matcher_config.get("validator", matcher_config.get("id", "")))
    else:
        matcher = str(matcher_config)
    tolerance = float(slot.get("tolerance", 1e-9))
    if matcher == "numeric":
        actual = parse_numeric_expression(value)
        expected = float(slot["value"])
        difference = abs(actual - expected)
        rounding_places = slot.get("round_to_decimal_places")
        rounding_meta: Dict[str, Any] = {}
        if rounding_places is not None:
            if (
                isinstance(rounding_places, bool)
                or not isinstance(rounding_places, int)
                or rounding_places < 0
            ):
                raise ValueError("round_to_decimal_places must be a non-negative integer")
            quantum = Decimal(1).scaleb(-rounding_places)
            try:
                rounded_actual = Decimal(str(actual)).quantize(quantum, rounding=ROUND_HALF_UP)
                rounded_expected = Decimal(str(expected)).quantize(quantum, rounding=ROUND_HALF_UP)
            except InvalidOperation as exc:
                raise ValueError("numeric answer cannot be rounded") from exc
            ok = rounded_actual == rounded_expected
            borderline = ok and difference > 1e-12
            borderline_candidate = not ok and difference <= float(quantum) * 2 + 1e-12
            rounding_meta = {
                "round_to_decimal_places": rounding_places,
                "rounding_mode": "ROUND_HALF_UP",
                "rounded_actual": str(rounded_actual),
                "rounded_expected": str(rounded_expected),
                "borderline_candidate": borderline_candidate,
            }
        else:
            ok = difference <= tolerance + 1e-12
            borderline = tolerance > 1e-6 and difference >= tolerance * 0.8
            borderline_candidate = not ok and tolerance > 1e-6 and difference <= tolerance * 2 + 1e-12
        return (
            ok,
            "numeric=%s" % actual,
            {
                "matcher": matcher,
                "expected": expected,
                "actual": actual,
                "difference": difference,
                "tolerance": tolerance,
                "borderline": borderline,
                "borderline_candidate": borderline_candidate,
                **rounding_meta,
            },
        )
    if matcher == "interval":
        actual = parse_interval(value)
        expected = (
            float(slot["low"]),
            float(slot["high"]),
            bool(slot["low_inclusive"]),
            bool(slot["high_inclusive"]),
        )
        if abs(actual[0] - expected[0]) > tolerance or abs(actual[1] - expected[1]) > tolerance:
            return False, "interval endpoints differ", {"matcher": matcher}
        if actual[2] != expected[2] or actual[3] != expected[3]:
            return False, "interval inclusion differs", {"matcher": matcher}
        return True, "interval", {"matcher": matcher}
    if matcher == "set_tokens":
        actual = parse_set_tokens(value)
        expected = tuple(sorted(slot["tokens"]))
        return actual == expected, "set=%s" % (actual,), {"matcher": matcher}
    if matcher == "vector_linear":
        actual = parse_vector_linear(value)
        expected = (float(slot["coefficient_a"]), float(slot["coefficient_b"]))
        if abs(actual[0] - expected[0]) <= tolerance and abs(actual[1] - expected[1]) <= tolerance:
            return True, "vector=(%s,%s)" % (actual[0], actual[1]), {"matcher": matcher}
        return False, "vector coefficients differ", {"matcher": matcher}
    if matcher == "exact":
        actual = _strip_text_answer(value)
        expected = str(slot["value"]).strip()
        return actual == expected, "exact=%s" % actual, {"matcher": matcher}
    if matcher == "normalized_text":
        actual = _clean_text(_strip_text_answer(value))
        expected = _clean_text(slot["value"])
        if not bool(slot.get("case_sensitive", True)):
            actual = actual.casefold()
            expected = expected.casefold()
        return actual == expected, "text=%s" % actual, {"matcher": matcher}
    raise ValueError("unknown matcher %r" % matcher)


def _validate_trig_pair(values: Sequence[Any]) -> bool:
    def angle(raw: Any) -> float:
        text = str(raw)
        parsed = parse_numeric_expression(text)
        if "°" in text or "度" in text:
            parsed = math.radians(parsed)
        return parsed

    alpha, beta = angle(values[0]), angle(values[1])
    if not (0 <= alpha <= 2 * math.pi + 1e-9 and 0 <= beta <= 2 * math.pi + 1e-9):
        return False
    left = math.sin(alpha + beta)
    right = math.sin(alpha - beta)
    cos_left = math.cos(alpha + beta)
    cos_right = math.cos(alpha - beta)
    return abs(left - right) <= 1e-8 and abs(cos_left - cos_right) > 1e-8


CUSTOM_VALIDATORS: Dict[str, Any] = {
    "beijing_q13_trig_pair": _validate_trig_pair,
}


def score_question(
    question: Mapping[str, Any],
    answer_key: Mapping[str, Any],
    raw_answer: Any,
    present: bool,
    *,
    answer_shape: Optional[str] = None,
    choice_labels: Sequence[str] = ("A", "B", "C", "D"),
) -> Dict[str, Any]:
    points = int(question["points"])
    result: Dict[str, Any] = {
        "question_id": question["id"],
        "paper_id": question["paper_id"],
        "points": points,
        "awarded": 0,
        "status": "missing",
        "reason": "no answer supplied",
        "normalized_answer": None,
    }
    if not present or raw_answer is None or (isinstance(raw_answer, str) and not raw_answer.strip()):
        return result

    try:
        shape = answer_shape or ("choice" if answer_key.get("correct_options") else "slots")
        if shape == "choice":
            option_labels = [
                str(option.get("label"))
                for option in (question.get("options") or [])
                if isinstance(option, Mapping) and option.get("label") is not None
            ]
            actual = parse_choice_set(raw_answer, option_labels or choice_labels)
            correct_options = answer_key.get("correct_options")
            if not isinstance(correct_options, (list, tuple)) or not correct_options:
                raise ValueError("choice answer key has no correct_options")
            expected = tuple(sorted(str(label) for label in correct_options))
            result["normalized_answer"] = "".join(actual)
            if actual == expected:
                result.update({"awarded": points, "status": "correct", "reason": "choice set exactly matches"})
            else:
                result.update({"status": "incorrect", "reason": "choice set differs from key"})
            return result

        if shape != "slots":
            raise ValueError("unsupported answer shape %r" % shape)
        slots = answer_key.get("slots") or []
        slot_count = int(question.get("answer_slot_count") or len(slots))
        if len(slots) != slot_count:
            raise ValueError("answer key slot count differs from question answer_slot_count")
        slot_values = _split_slots(raw_answer, slot_count)
        custom = answer_key.get("custom_validator")
        if custom:
            validator = CUSTOM_VALIDATORS.get(str(custom))
            if validator is None:
                raise ValueError("unknown custom validator %r" % custom)
            ok = validator(slot_values)
            result["normalized_answer"] = "；".join(str(item) for item in slot_values)
            if ok:
                result.update({"awarded": points, "status": "correct", "reason": "custom validator %s accepted" % custom})
            else:
                result.update({"status": "incorrect", "reason": "custom validator %s rejected" % custom})
            return result

        normalized: List[str] = []
        slot_matches: List[Dict[str, Any]] = []
        for slot_index, (slot_value, slot) in enumerate(zip(slot_values, slots), 1):
            ok, detail, match_meta = _match_slot(slot_value, slot)
            normalized.append(detail)
            slot_matches.append({"slot": slot_index, **match_meta})
            if not ok:
                result["normalized_answer"] = "；".join(normalized)
                result["slot_matches"] = slot_matches
                borderline_candidate = any(
                    bool(item.get("borderline_candidate")) for item in slot_matches
                )
                result["borderline"] = False
                result["borderline_candidate"] = borderline_candidate
                result.update({"status": "incorrect", "reason": "slot %s differs" % (len(normalized))})
                return result
        result["normalized_answer"] = "；".join(normalized)
        result["slot_matches"] = slot_matches
        borderline = any(bool(item.get("borderline")) for item in slot_matches)
        result.update(
            {
                "awarded": points,
                "status": "correct",
                "borderline": borderline,
                "borderline_candidate": False,
                "reason": "all answer slots match within a borderline tolerance" if borderline else "all answer slots match",
            }
        )
        return result
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        result.update({"status": "parse_error", "reason": str(exc)})
        return result


def _metric(
    metric_id: str,
    *,
    metric_version: str,
    numerator: float,
    denominator: float,
    numerator_unit: str,
    scope: str,
    definition: str,
    coverage: Mapping[str, Any],
    role: str = "diagnostic",
) -> Dict[str, Any]:
    return {
        "id": metric_id,
        "version": metric_version,
        "unit": "ratio",
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
        "numerator_unit": numerator_unit,
        "definition": definition,
        "scope": scope,
        "role": role,
        "coverage": dict(coverage),
    }


def evaluate_submission(
    dataset: Dataset,
    answers: Mapping[str, Any],
    *,
    question_ids: Optional[Sequence[str]] = None,
    view_id: str = "full",
    coverage_label: Optional[str] = None,
) -> Dict[str, Any]:
    """Score a submission with an explicit, versioned metric scope.

    ``question_ids`` limits the reported run to a selected subset.  The primary
    metric always uses that subset's reviewed possible points as its denominator
    and is labelled ``selected_subset``.  A full-benchmark score is only reported
    when the selected IDs cover the complete benchmark view.
    """

    if not isinstance(answers, Mapping):
        raise ValueError("submission 'answers' must be an object keyed by question ID")

    view_questions = list(dataset.spec.select_view(view_id, dataset.questions))
    view_ids = {question["id"] for question in view_questions}
    selected_ids_arg: Optional[set[str]] = None
    if question_ids is not None:
        selected_ids_arg = {str(qid) for qid in question_ids}
        if not selected_ids_arg:
            raise ValueError("question_ids must not be empty")
        unknown_selection = sorted(selected_ids_arg - view_ids)
        if unknown_selection:
            raise ValueError("question_ids are outside benchmark view %s: %s" % (view_id, ", ".join(unknown_selection)))
        selected_questions = [q for q in view_questions if q["id"] in selected_ids_arg]
    else:
        selected_questions = view_questions

    selected_ids = {q["id"] for q in selected_questions}
    scope = "full_view" if selected_ids == view_ids else "selected_subset"
    if selected_ids == {q["id"] for q in dataset.questions}:
        scope = "full_dataset"

    results: List[Dict[str, Any]] = []
    known_ids = {q["id"] for q in dataset.questions}
    for question in selected_questions:
        qid = question["id"]
        present = qid in answers
        shape_spec = dataset.spec.answer_shape(question_type(question))
        results.append(
            score_question(
                question,
                dataset.answer_keys[qid],
                answers.get(qid),
                present,
                answer_shape=shape_spec.shape if shape_spec is not None else None,
                choice_labels=dataset.spec.choice_labels,
            )
        )

    paper_summaries: Dict[str, Dict[str, Any]] = {}
    for paper in dataset.papers:
        paper_id = paper["id"]
        paper_results = [row for row in results if row["paper_id"] == paper_id]
        if not paper_results:
            continue
        earned = sum(int(row["awarded"]) for row in paper_results)
        possible = sum(int(row["points"]) for row in paper_results)
        correct = len([row for row in paper_results if row["status"] == "correct"])
        paper_summaries[paper_id] = {
            "title": paper["title"],
            "questions": len(paper_results),
            "correct_questions": correct,
            "earned_points": earned,
            "possible_points": possible,
            "point_accuracy": earned / possible if possible else 0.0,
            "exact_accuracy": correct / len(paper_results) if paper_results else 0.0,
        }

    total_awarded = sum(int(row["awarded"]) for row in results)
    total_possible = sum(int(row["points"]) for row in results)
    correct_questions = len([row for row in results if row["status"] == "correct"])
    failure_counts: Dict[str, int] = {}
    for row in results:
        failure_counts[row["status"]] = failure_counts.get(row["status"], 0) + 1

    coverage = {
        "label": coverage_label or ("view:%s" % view_id),
        "selected_questions": len(selected_questions),
        "view_questions": len(view_questions),
        "dataset_questions": len(dataset.questions),
        "selected_possible_points": total_possible,
        "view_possible_points": sum(int(q["points"]) for q in view_questions),
        "dataset_possible_points": sum(int(q["points"]) for q in dataset.questions),
        "selected_question_ids": sorted(selected_ids),
    }
    metric_version = dataset.spec.scoring.metric_version
    weighted_points = _metric(
        dataset.spec.scoring.primary_metric,
        metric_version=metric_version,
        numerator=total_awarded,
        denominator=total_possible,
        numerator_unit="points",
        scope=scope,
        definition="earned reviewed exam points divided by possible points in the declared scope",
        coverage=coverage,
        role="primary",
    )
    exact_accuracy = _metric(
        "diagnostic.exact_accuracy",
        metric_version=metric_version,
        numerator=correct_questions,
        denominator=len(results),
        numerator_unit="questions",
        scope=scope,
        definition="fully correct questions divided by questions in the declared scope",
        coverage=coverage,
        role="diagnostic",
    )
    paper_accuracies = [paper_summaries[paper_id]["point_accuracy"] for paper_id in paper_summaries]
    paper_equal = _metric(
        "diagnostic.paper_equal_weighted_points",
        metric_version=metric_version,
        numerator=sum(paper_accuracies),
        denominator=len(paper_accuracies),
        numerator_unit="paper_ratio_sum",
        scope=scope,
        definition="equal-weight mean of paper point accuracies; diagnostic only, never the primary score",
        coverage=coverage,
        role="diagnostic",
    )

    required_ids = {
        str(q["id"]) for q in selected_questions if question_requires_vision(q)
    }
    text_rows = [row for row in results if row["question_id"] not in required_ids]
    text_awarded = sum(int(row["awarded"]) for row in text_rows)
    text_possible = sum(int(row["points"]) for row in text_rows)
    vision_rows = [row for row in results if row["question_id"] in required_ids]
    vision_awarded = sum(int(row["awarded"]) for row in vision_rows)
    vision_possible = sum(int(row["points"]) for row in vision_rows)
    slices = {
        "text_answerable": _metric(
            "slice.text_answerable.weighted_points",
            metric_version=metric_version,
            numerator=text_awarded,
            denominator=text_possible,
            numerator_unit="points",
            scope=scope,
            definition="score over questions that do not require an attached figure",
            coverage={
                **coverage,
                "slice_questions": len(text_rows),
                "slice_possible_points": text_possible,
            },
            role="diagnostic",
        ),
        "vision_required": _metric(
            "slice.vision_required.weighted_points",
            metric_version=metric_version,
            numerator=vision_awarded,
            denominator=vision_possible,
            numerator_unit="points",
            scope=scope,
            definition="score over questions whose prompt requires an attached figure",
            coverage={
                **coverage,
                "slice_questions": len(vision_rows),
                "slice_possible_points": vision_possible,
                "minimum_questions_for_standalone_ranking": dataset.spec.reporting.min_visual_questions_for_ranking,
                "small_sample_warning": len(vision_rows) < dataset.spec.reporting.min_visual_questions_for_ranking,
            },
            role=dataset.spec.reporting.visual_slice_role,
        ),
        "text_on_scope_denominator": _metric(
            "slice.text_on_scope_denominator.weighted_points",
            metric_version=metric_version,
            numerator=text_awarded,
            denominator=total_possible,
            numerator_unit="points",
            scope=scope,
            definition="text-answerable earned points divided by the full selected-scope denominator",
            coverage=coverage,
            role="diagnostic",
        ),
    }

    unexpected_ids = sorted(set(answers) - known_ids)
    out_of_scope_ids = sorted((set(answers) & known_ids) - selected_ids)
    borderline_ids = sorted(
        row["question_id"] for row in results if bool(row.get("borderline"))
    )
    borderline_candidate_ids = sorted(
        row["question_id"] for row in results if bool(row.get("borderline_candidate"))
    )
    metrics = {
        weighted_points["id"]: weighted_points,
        exact_accuracy["id"]: exact_accuracy,
        paper_equal["id"]: paper_equal,
        **{metric["id"]: metric for metric in slices.values()},
    }
    return {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "dataset": "Gaokao-Bench",
        "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
        "benchmark_spec": {
            "id": dataset.spec.id,
            "version": dataset.spec.version,
            "sha256": dataset.spec.fingerprint(),
        },
        "scoring_profile": dataset.spec.scoring.profile,
        "scorer_version": SCORER_VERSION,
        "scope": {
            "view": view_id,
            "kind": scope,
            "coverage": coverage,
        },
        "primary_metric": weighted_points,
        "metrics": metrics,
        "summary": {
            "earned_points": total_awarded,
            "possible_points": total_possible,
            "correct_questions": correct_questions,
            "borderline_questions": len(borderline_ids),
            "borderline_question_ids": borderline_ids,
            "borderline_candidate_questions": len(borderline_candidate_ids),
            "borderline_candidate_question_ids": borderline_candidate_ids,
            "total_questions": len(results),
            "failure_counts": failure_counts,
            "primary_metric": weighted_points,
            "slices": slices,
            "papers": paper_summaries,
        },
        "reporting": {
            "leaderboard_class": dataset.spec.reporting.leaderboard_class,
            "freshness_claim_allowed": dataset.spec.reporting.freshness_claim_allowed,
            "held_out": dataset.spec.reporting.held_out,
            "canonical_min_repeats": dataset.spec.reporting.canonical_min_repeats,
            "canonical_timeout_seconds": dataset.spec.reporting.canonical_timeout_seconds,
            "canonical_max_tokens": dataset.spec.reporting.canonical_max_tokens,
            "constrained_reasoning_budget": dataset.spec.reporting.constrained_reasoning_budget,
            "min_visual_questions_for_ranking": dataset.spec.reporting.min_visual_questions_for_ranking,
            "visual_slice_role": dataset.spec.reporting.visual_slice_role,
            "rounding_policy": dataset.spec.reporting.rounding_policy,
            "live_track_required": dataset.spec.reporting.live_track_required,
            "tracks": [
                {
                    "id": dataset.spec.reporting.leaderboard_class,
                    "status": "active",
                    "freshness_claim_allowed": dataset.spec.reporting.freshness_claim_allowed,
                    "held_out": dataset.spec.reporting.held_out,
                },
                {
                    "id": "post_cutoff_live",
                    "status": "required" if dataset.spec.reporting.live_track_required else "optional",
                    "freshness_claim_allowed": True,
                    "held_out": True,
                },
            ],
        },
        "unexpected_answer_ids": unexpected_ids,
        "out_of_scope_answer_ids": out_of_scope_ids,
        "results": results,
    }


def canonical_submission(dataset: Dataset) -> Dict[str, Any]:
    answers: Dict[str, Any] = {}
    for question in dataset.questions:
        key = dataset.answer_keys[question["id"]]
        shape = dataset.spec.answer_shape(question_type(question))
        if shape is None:
            raise ValueError("question type is not declared by benchmark spec: %s" % question["id"])
        if shape.shape == "choice":
            answers[question["id"]] = "".join(key["correct_options"])
        elif shape.shape == "slots":
            slots = [slot["canonical"] for slot in key["slots"]]
            answers[question["id"]] = slots[0] if len(slots) == 1 else slots
        else:
            raise ValueError("canonical subjective answers require the rubric engine: %s" % question["id"])
    return {"answers": answers}

