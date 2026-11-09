"""Deterministic alpha scorer for Gaokao-Bench.

The public contract is intentionally narrow:

* a submission is ``{"answers": {question_id: answer, ...}}``;
* every question is all-or-nothing in 0.0.1;
* missing, malformed, empty, and incorrect answers receive zero;
* the weighted denominator is the sum of reviewed per-question points;
  those points are media-crosschecked in this alpha and remain pending
  owner signoff.

No model output is interpreted with an LLM.  LaTeX / Unicode math is
normalised with a small, auditable expression parser.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Default version for the checked-in alpha package.  Dataset validation uses
# the version recorded in manifest.json so signed review materialisation can
# produce a new immutable patch version without editing the scorer.
DATASET_VERSION = "0.0.1"
SCHEMA_VERSION = "1.0"
SCORER_VERSION = "0.0.1"
SCORING_PROFILE = "strict-source-audited-points-all-or-nothing"
EXPECTED_PAPER_POINTS = {
    "GB25-N1": 73,
    "GB25-N2": 73,
    "GB25-BJ": 60,
    "GB25-SH": 72,
    "GB25-TJ": 75,
}
EXPECTED_TOTAL_POINTS = 353
EXPECTED_QUESTION_COUNT = 74
EXPECTED_CHOICE_COUNT = 45
EXPECTED_FILL_COUNT = 29
EXPECTED_CATALOG_SUBJECTIVE_COUNT = 26
EXPECTED_CATALOG_SUBJECTIVE_POINTS = 397
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
}
QUESTION_STEM_VERDICTS = {"pdf_exact", "pdf_corrected"}
QUESTION_OPTION_VERDICTS = {"pdf_exact", "pdf_corrected", "not_applicable"}
QUESTION_ASSET_VERDICTS = {"text_sufficient", "figure_required"}
QUESTION_ANSWER_VERDICTS = {"confirmed"}
EXPECTED_REQUIRED_IMAGE_IDS = {
    "GB25-N1-Q06",
    "GB25-BJ-Q14",
    "GB25-SH-Q11",
    "GB25-TJ-Q03",
}
PUBLIC_PACKAGE_PROFILE = "public-data"
PUBLIC_REQUIRED_FILES = {
    ".gitattributes",
    "README.md",
    "CITATION.cff",
    "LICENSE-DATA",
    "THIRD_PARTY_NOTICES.md",
    "answer_keys.jsonl",
    "assets.jsonl",
    "catalog/subjective_catalog.jsonl",
    "examples/README.md",
    "examples/empty.json",
    "examples/gold.json",
    "examples/malformed.json",
    "papers.jsonl",
    "questions.jsonl",
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


def _validate_source_assessments(
    row: Mapping[str, Any],
    answer_sources: Mapping[str, Mapping[str, Any]],
    errors: List[str],
) -> None:
    """Validate question-level source assessments and the Q13 evidence gate."""

    qid = str(row.get("question_id", ""))
    assessments = row.get("source_assessments")
    if assessments is None:
        if qid == "GB25-N1-Q13":
            errors.append("Q13 must carry source assessments with stem-consistency decisions")
        return
    if not isinstance(assessments, list) or not assessments:
        errors.append("source assessments must be a non-empty list for %s" % qid)
        return

    seen: set[str] = set()
    valid: List[Mapping[str, Any]] = []
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
        valid.append(item)

    if qid != "GB25-N1-Q13":
        return

    supporting = [item for item in valid if item.get("role") == "supporting"]
    rejected = [item for item in valid if item.get("role") == "rejected"]
    exact = [item for item in supporting if item.get("stem_match") == "exact_stem_match"]
    publishers = {str(item.get("publisher", "")).strip() for item in supporting}
    independence_groups = {
        str(item.get("independence_group", "")).strip() for item in supporting
    }
    if str(row.get("canonical") if "canonical" in row else row.get("reviewed_canonical")) != "2":
        errors.append("Q13 canonical answer must be 2")
    if row.get("status") not in {
        "source_and_derivation_confirmed",
        "owner_and_two_independent_reviewers_complete",
    }:
        errors.append(
            "Q13 audit must retain source-and-derivation evidence before or after signoff"
        )
    if str(row.get("external_canonical")) != "2" or str(row.get("secondary_external_canonical")) != "2":
        errors.append("Q13 visible source answer values must both be 2")
    if len(supporting) < 4:
        errors.append("Q13 requires at least four supporting source assessments")
    if len(exact) < 3:
        errors.append("Q13 requires at least three exact-stem source assessments")
    if len(publishers) < 3:
        errors.append("Q13 requires supporting evidence from at least three publishers")
    if len(independence_groups) < 3:
        errors.append("Q13 requires supporting evidence from at least three independence groups")
    if any(item.get("stem_match") in ("variant_missing_positive_condition", "stem_not_visible") for item in supporting):
        errors.append("Q13 supporting evidence includes a source without a matching stem")
    rejected_ids = {str(item.get("source_id")) for item in rejected}
    rejected_reasons = {str(item.get("stem_match")) for item in rejected}
    required_rejections = {"stem_not_visible", "variant_missing_positive_condition"}
    if len(rejected) < 2 or not required_rejections <= rejected_reasons:
        errors.append(
            "Q13 must preserve rejected sources for an invisible stem and a variant without the positive condition"
        )
    declared_rejected = {str(source_id) for source_id in (row.get("rejected_source_ids") or [])}
    if declared_rejected != rejected_ids:
        errors.append("Q13 rejected_source_ids must match rejected source assessments")
    supporting_ids = {
        str(row.get("external_source_id")),
        str(row.get("secondary_source_id")),
        *(str(source_id) for source_id in (row.get("supporting_source_ids") or [])),
    }
    assessed_supporting_ids = {str(item.get("source_id")) for item in supporting}
    if supporting_ids != assessed_supporting_ids:
        errors.append("Q13 declared supporting source IDs must match supporting assessments")
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
    papers = tuple(_read_jsonl(root / "papers.jsonl"))
    questions = tuple(_read_jsonl(root / "questions.jsonl"))
    answer_keys_list = _read_jsonl(root / "answer_keys.jsonl")
    answer_keys = {row["question_id"]: row for row in answer_keys_list}
    catalog_subjective = tuple(_read_jsonl(root / "catalog" / "subjective_catalog.jsonl"))
    dataset = Dataset(root, manifest, papers, questions, answer_keys, catalog_subjective)
    if validate:
        validate_dataset(dataset)
    return dataset


def _validate_public_manifest(dataset: Dataset) -> None:
    errors: List[str] = []
    manifest = dataset.manifest
    if manifest.get("package_profile") != PUBLIC_PACKAGE_PROFILE:
        errors.append("public package_profile must be %s" % PUBLIC_PACKAGE_PROFILE)
    if manifest.get("public_release") is not True:
        errors.append("public dataset must set public_release=true")
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    if str(rights.get("public_redistribution", "")).startswith("not_cleared"):
        errors.append("public dataset still declares that redistribution is not cleared")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        errors.append("public manifest files must be a non-empty object")
        files = {}
    missing_files = sorted(PUBLIC_REQUIRED_FILES - set(files))
    if missing_files:
        errors.append("public manifest does not cover required files: %s" % ", ".join(missing_files))
    forbidden: List[str] = []
    for relative_path in files:
        if relative_path in PUBLIC_FORBIDDEN_PATHS or relative_path.startswith(
            ("docs/", "reports/", "review_packets/", "runs/", "release/", "review_ui/", "scripts/", "tests/")
        ):
            forbidden.append(relative_path)
    if forbidden:
        errors.append("public manifest contains internal paths: %s" % ", ".join(sorted(forbidden)[:5]))

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
        if metadata.get("bytes") != path.stat().st_size:
            errors.append("public manifest byte count mismatch: %s" % relative_path)
        if metadata.get("sha256") != file_sha256(path):
            errors.append("public manifest hash mismatch: %s" % relative_path)

    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    expected_counts = {
        "papers": len(dataset.papers),
        "objective_questions": len(dataset.questions),
        "choice_questions": len([q for q in dataset.questions if q["type"] in ("single_choice", "multi_choice")]),
        "multi_choice_questions": len([q for q in dataset.questions if q["type"] == "multi_choice"]),
        "fill_questions": len([q for q in dataset.questions if q["type"] == "fill_blank"]),
        "answer_slots": sum(int(q["answer_slot_count"]) for q in dataset.questions),
        "image_required_questions": len([q for q in dataset.questions if q.get("asset_dependency") == "required"]),
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
    dataset_version = dataset.manifest.get("dataset_version")
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
    question_ids = [q["id"] for q in dataset.questions]
    if len(question_ids) != len(set(question_ids)):
        errors.append("question IDs are not unique")

    if len(dataset.questions) != EXPECTED_QUESTION_COUNT:
        errors.append("expected %d questions, got %d" % (EXPECTED_QUESTION_COUNT, len(dataset.questions)))
    if len(dataset.papers) != 5:
        errors.append("expected 5 papers, got %d" % len(dataset.papers))
    if sum(int(paper["full_question_count"]) for paper in dataset.papers) != 100:
        errors.append("full question count must close at 100")
    if sum(int(paper["full_paper_points"]) for paper in dataset.papers) != 750:
        errors.append("full paper points must close at 750")

    choices = [q for q in dataset.questions if q["type"] in ("single_choice", "multi_choice")]
    fills = [q for q in dataset.questions if q["type"] == "fill_blank"]
    if len(choices) != EXPECTED_CHOICE_COUNT:
        errors.append("expected %d choice questions, got %d" % (EXPECTED_CHOICE_COUNT, len(choices)))
    if len(fills) != EXPECTED_FILL_COUNT:
        errors.append("expected %d fill questions, got %d" % (EXPECTED_FILL_COUNT, len(fills)))

    total_points = sum(int(q["points"]) for q in dataset.questions)
    if total_points != EXPECTED_TOTAL_POINTS:
        errors.append("expected %d objective points, got %d" % (EXPECTED_TOTAL_POINTS, total_points))

    paper_points: Dict[str, int] = {}
    for paper in dataset.papers:
        paper_id = paper["id"]
        selected = [q for q in dataset.questions if q["paper_id"] == paper_id]
        points = sum(int(q["points"]) for q in selected)
        paper_points[paper_id] = points
        if EXPECTED_PAPER_POINTS.get(paper_id) != points:
            errors.append(
                "paper %s objective points: expected %s, got %s"
                % (paper_id, EXPECTED_PAPER_POINTS.get(paper_id), points)
            )
        if int(paper["objective_points"]) != points:
            errors.append("paper %s objective_points field is stale" % paper_id)
        if int(paper["objective_questions"]) != len(selected):
            errors.append("paper %s objective_questions field is stale" % paper_id)

    if set(paper_points) != set(EXPECTED_PAPER_POINTS):
        errors.append("paper IDs do not match expected IDs")

    catalog_subjective_points = sum(int(q["points"]) for q in dataset.catalog_subjective)
    if len(dataset.catalog_subjective) != EXPECTED_CATALOG_SUBJECTIVE_COUNT:
        errors.append(
            "expected %d catalogued subjective questions, got %d"
            % (EXPECTED_CATALOG_SUBJECTIVE_COUNT, len(dataset.catalog_subjective))
        )
    if catalog_subjective_points != EXPECTED_CATALOG_SUBJECTIVE_POINTS:
        errors.append(
            "expected %d catalogued subjective points, got %d"
            % (EXPECTED_CATALOG_SUBJECTIVE_POINTS, catalog_subjective_points)
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
        if int(paper.get("catalog_subjective_questions", -1)) != len(catalog_rows):
            errors.append("paper %s catalog_subjective_questions field is stale" % paper_id)
        if int(paper.get("catalog_subjective_points", -1)) != catalog_points:
            errors.append("paper %s catalog_subjective_points field is stale" % paper_id)
        if paper_points[paper_id] + catalog_points != int(paper["full_paper_points"]):
            errors.append("paper %s scored plus catalogued points do not close" % paper_id)

    for question in dataset.questions:
        qid = question["id"]
        key = dataset.answer_keys.get(qid)
        if key is None:
            errors.append("missing answer key for %s" % qid)
            continue
        if int(key["points"]) != int(question["points"]):
            errors.append("answer-key points mismatch for %s" % qid)
        if key["kind"] != question["type"]:
            errors.append("answer-key kind mismatch for %s" % qid)
        if question["type"] in ("single_choice", "multi_choice"):
            options = question.get("options", [])
            if [option.get("label") for option in options] != ["A", "B", "C", "D"]:
                errors.append("choice options must be A-D in order: %s" % qid)
            if any(not str(option.get("text", "")).strip() for option in options):
                errors.append("choice option text is empty: %s" % qid)
            if not str(question.get("stem", "")).strip():
                errors.append("choice stem is empty: %s" % qid)
            labels = key.get("correct_options", [])
            expected_label_count = 1 if question["type"] == "single_choice" else None
            if expected_label_count is not None and len(labels) != expected_label_count:
                errors.append("single-choice key must contain one option: %s" % qid)
            if question["type"] == "multi_choice" and len(labels) < 2:
                errors.append("multi-choice key must contain at least two options: %s" % qid)
            if any(label not in "ABCD" for label in labels):
                errors.append("invalid option label for %s" % qid)
            if key.get("canonical") != "".join(sorted(labels)):
                errors.append("choice canonical does not match sorted options for %s" % qid)
        else:
            slots = key.get("slots", [])
            if not slots:
                errors.append("fill answer key has no slots: %s" % qid)
            if int(question["answer_slot_count"]) != len(slots):
                errors.append("answer slot count mismatch for %s" % qid)
            expected_canonical = [slot.get("canonical") for slot in slots]
            if len(expected_canonical) == 1:
                expected_canonical = expected_canonical[0]
            if key.get("canonical") != expected_canonical:
                errors.append("fill canonical does not match answer slots for %s" % qid)
        image_path = question.get("image_path")
        if image_path:
            asset = dataset.data_dir / image_path
            if not asset.is_file():
                errors.append("missing image asset for %s: %s" % (qid, image_path))
            elif question.get("image_sha256"):
                if file_sha256(asset) != question["image_sha256"]:
                    errors.append("image hash mismatch for %s" % qid)

    for key_id in dataset.answer_keys:
        if key_id not in set(question_ids):
            errors.append("orphan answer key: %s" % key_id)

    required_assets = [q for q in dataset.questions if q.get("asset_dependency") == "required"]
    required_asset_ids = {q["id"] for q in required_assets}
    if required_asset_ids != EXPECTED_REQUIRED_IMAGE_IDS:
        errors.append(
            "image-required question set differs: expected %s, got %s"
            % (sorted(EXPECTED_REQUIRED_IMAGE_IDS), sorted(required_asset_ids))
        )
    if dataset.manifest.get("package_profile") == PUBLIC_PACKAGE_PROFILE:
        if errors:
            raise ValueError("Gaokao-Bench dataset validation failed:\n- " + "\n- ".join(errors))
        _validate_public_manifest(dataset)
        return {
            "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
            "papers": len(dataset.papers),
            "questions": len(dataset.questions),
            "choice_questions": len(choices),
            "fill_questions": len(fills),
            "multi_choice_questions": len([q for q in choices if q["type"] == "multi_choice"]),
            "answer_slots": sum(int(q["answer_slot_count"]) for q in dataset.questions),
            "objective_points": total_points,
            "paper_points": paper_points,
            "scored_scope": "choice_and_fill_only",
            "catalog_subjective_questions": len(dataset.catalog_subjective),
            "catalog_subjective_points": catalog_subjective_points,
            "image_required_questions": len(required_assets),
            "question_audit_rows": 0,
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
        if row.get("question_type") != question.get("type"):
            errors.append("answer audit question type mismatch for %s" % qid)
        if row.get("current_canonical") != key.get("canonical"):
            errors.append("answer audit current canonical differs from answer key for %s" % qid)
        if row.get("reviewed_canonical") != key.get("canonical"):
            errors.append("answer audit reviewed canonical differs from answer key for %s" % qid)
        if row.get("status") not in ANSWER_AUDIT_STATUSES:
            errors.append("answer audit has unknown status for %s: %s" % (qid, row.get("status")))
        if row.get("human_independent_review") not in ("pending", "complete"):
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
        if row.get("kind") != question.get("type"):
            errors.append("point audit kind mismatch for %s" % qid)
        if row.get("current_points") != question.get("points") or row.get("reviewed_points") != question.get("points"):
            errors.append("point audit does not match question points for %s" % qid)
        if row.get("delta") != 0:
            errors.append("point audit current delta must be zero for %s" % qid)
        if row.get("status") not in POINT_AUDIT_STATUSES:
            errors.append("point audit has unknown status for %s: %s" % (qid, row.get("status")))
        if row.get("human_independent_review") not in ("pending", "complete"):
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
        if len(question_audit) != EXPECTED_QUESTION_COUNT:
            errors.append("expected %d question audit rows, got %d" % (EXPECTED_QUESTION_COUNT, len(question_audit)))
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
            if row.get("review_status") != "independently_derived_pending_owner_signoff":
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
    for field, expected in expected_counts.items():
        if int(counts.get(field, -1)) != expected:
            errors.append("manifest count %s is stale: expected %d, got %s" % (field, expected, counts.get(field)))

    quality = dataset.manifest.get("quality") if isinstance(dataset.manifest.get("quality"), dict) else {}
    review_lineage = dataset.manifest.get("review_lineage")
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

    required_assets = [q for q in dataset.questions if q.get("asset_dependency") == "required"]
    required_asset_ids = {q["id"] for q in required_assets}
    if required_asset_ids != EXPECTED_REQUIRED_IMAGE_IDS:
        errors.append(
            "image-required question set differs: expected %s, got %s"
            % (sorted(EXPECTED_REQUIRED_IMAGE_IDS), sorted(required_asset_ids))
        )

    if errors:
        raise ValueError("Gaokao-Bench dataset validation failed:\n- " + "\n- ".join(errors))

    return {
        "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
        "papers": len(dataset.papers),
        "questions": len(dataset.questions),
        "choice_questions": len(choices),
        "fill_questions": len(fills),
        "multi_choice_questions": len([q for q in choices if q["type"] == "multi_choice"]),
        "answer_slots": sum(int(q["answer_slot_count"]) for q in dataset.questions),
        "objective_points": total_points,
        "paper_points": paper_points,
        "scored_scope": "choice_and_fill_only",
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


_CHOICE_RE = re.compile(r"^[A-Da-d\s,，、;；()（）]+$")


def parse_choice_set(value: Any) -> Tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        if not value:
            raise ValueError("empty choice answer")
        items = [_clean_text(item).upper() for item in value]
        if any(item not in ("A", "B", "C", "D") for item in items):
            raise ValueError("choice list contains an invalid option")
        return tuple(sorted(set(items)))
    text = _strip_answer_prefix(value)
    text = text.replace("。", "").replace(".", "")
    if not text or not _CHOICE_RE.fullmatch(text):
        raise ValueError("choice answer must contain only A-D separators")
    letters = re.findall(r"[A-Da-d]", text)
    if not letters:
        raise ValueError("empty choice answer")
    return tuple(sorted(set(letter.upper() for letter in letters)))


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


def _match_slot(value: Any, slot: Mapping[str, Any]) -> Tuple[bool, str]:
    matcher = slot["matcher"]
    tolerance = float(slot.get("tolerance", 1e-9))
    if matcher == "numeric":
        actual = parse_numeric_expression(value)
        expected = float(slot["value"])
        return abs(actual - expected) <= tolerance, "numeric=%s" % actual
    if matcher == "interval":
        actual = parse_interval(value)
        expected = (
            float(slot["low"]),
            float(slot["high"]),
            bool(slot["low_inclusive"]),
            bool(slot["high_inclusive"]),
        )
        if abs(actual[0] - expected[0]) > tolerance or abs(actual[1] - expected[1]) > tolerance:
            return False, "interval endpoints differ"
        if actual[2] != expected[2] or actual[3] != expected[3]:
            return False, "interval inclusion differs"
        return True, "interval"
    if matcher == "set_tokens":
        actual = parse_set_tokens(value)
        expected = tuple(sorted(slot["tokens"]))
        return actual == expected, "set=%s" % (actual,)
    if matcher == "vector_linear":
        actual = parse_vector_linear(value)
        expected = (float(slot["coefficient_a"]), float(slot["coefficient_b"]))
        if abs(actual[0] - expected[0]) <= tolerance and abs(actual[1] - expected[1]) <= tolerance:
            return True, "vector=(%s,%s)" % (actual[0], actual[1])
        return False, "vector coefficients differ"
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


def score_question(question: Mapping[str, Any], answer_key: Mapping[str, Any], raw_answer: Any, present: bool) -> Dict[str, Any]:
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
        if question["type"] in ("single_choice", "multi_choice"):
            actual = parse_choice_set(raw_answer)
            expected = tuple(sorted(answer_key["correct_options"]))
            result["normalized_answer"] = "".join(actual)
            if actual == expected:
                result.update({"awarded": points, "status": "correct", "reason": "choice set exactly matches"})
            else:
                result.update({"status": "incorrect", "reason": "choice set differs from key"})
            return result

        slot_count = int(question["answer_slot_count"])
        slot_values = _split_slots(raw_answer, slot_count)
        custom = answer_key.get("custom_validator")
        if custom == "beijing_q13_trig_pair":
            ok = _validate_trig_pair(slot_values)
            result["normalized_answer"] = "；".join(str(item) for item in slot_values)
            if ok:
                result.update({"awarded": points, "status": "correct", "reason": "valid trigonometric pair"})
            else:
                result.update({"status": "incorrect", "reason": "pair does not satisfy the stated constraints"})
            return result
        if custom:
            raise ValueError("unknown custom validator %r" % custom)

        normalized: List[str] = []
        for slot_value, slot in zip(slot_values, answer_key["slots"]):
            ok, detail = _match_slot(slot_value, slot)
            normalized.append(detail)
            if not ok:
                result["normalized_answer"] = "；".join(normalized)
                result.update({"status": "incorrect", "reason": "slot %s differs" % (len(normalized))})
                return result
        result["normalized_answer"] = "；".join(normalized)
        result.update({"awarded": points, "status": "correct", "reason": "all answer slots match"})
        return result
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        result.update({"status": "parse_error", "reason": str(exc)})
        return result


def evaluate_submission(dataset: Dataset, answers: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(answers, Mapping):
        raise ValueError("submission 'answers' must be an object keyed by question ID")

    results: List[Dict[str, Any]] = []
    known_ids = {q["id"] for q in dataset.questions}
    for question in dataset.questions:
        qid = question["id"]
        present = qid in answers
        results.append(score_question(question, dataset.answer_keys[qid], answers.get(qid), present))

    paper_summaries: Dict[str, Dict[str, Any]] = {}
    for paper in dataset.papers:
        paper_id = paper["id"]
        paper_results = [row for row in results if row["paper_id"] == paper_id]
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
    paper_accuracies = [paper_summaries[p]["point_accuracy"] for p in EXPECTED_PAPER_POINTS]
    failure_counts: Dict[str, int] = {}
    for row in results:
        failure_counts[row["status"]] = failure_counts.get(row["status"], 0) + 1

    unexpected_ids = sorted(set(answers) - known_ids)
    return {
        "dataset": "Gaokao-Bench",
        "dataset_version": dataset.manifest.get("dataset_version", DATASET_VERSION),
        "scoring_profile": SCORING_PROFILE,
        "scorer_version": SCORER_VERSION,
        "summary": {
            "metrics": {
                "GB-2025-Math-Obj": sum(paper_accuracies) / len(paper_accuracies),
                "GB-2025-Math-Obj-Weighted": total_awarded / total_possible if total_possible else 0.0,
                "GB-2025-Math-Exact": correct_questions / len(results) if results else 0.0,
            },
            "GB-2025-Math-Obj": 100.0 * sum(paper_accuracies) / len(paper_accuracies),
            "GB-2025-Math-Obj-Weighted": 100.0 * total_awarded / total_possible if total_possible else 0.0,
            "GB-2025-Math-Exact": 100.0 * correct_questions / len(results) if results else 0.0,
            "earned_points": total_awarded,
            "possible_points": total_possible,
            "correct_questions": correct_questions,
            "total_questions": len(results),
            "failure_counts": failure_counts,
            "papers": paper_summaries,
        },
        "unexpected_answer_ids": unexpected_ids,
        "results": results,
    }


def canonical_submission(dataset: Dataset) -> Dict[str, Any]:
    answers: Dict[str, Any] = {}
    for question in dataset.questions:
        key = dataset.answer_keys[question["id"]]
        if question["type"] in ("single_choice", "multi_choice"):
            answers[question["id"]] = "".join(key["correct_options"])
        else:
            slots = [slot["canonical"] for slot in key["slots"]]
            answers[question["id"]] = slots[0] if len(slots) == 1 else slots
    return {"answers": answers}

