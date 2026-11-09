"""Materialise completed human review into a new immutable dataset version.

The review import commands are deliberately read-only.  This module is the
only write path: it revalidates the original owner and independent-review
inputs, refuses proposed answer/point changes, copies the frozen dataset, and
writes a new version whose manifest binds the signed evidence by hashes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .owner_review import import_owner_signoff
from .review import import_review_packet
from .scoring import SCORER_VERSION, Dataset, evaluate_submission, load_dataset, validate_dataset


MATERIALIZATION_SCHEMA_VERSION = "1.0"
SIGNED_REVIEW_STATUS = "owner_and_two_independent_reviewers_complete"
MATERIALIZATION_DOCUMENT = "REVIEW_MATERIALIZATION.md"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z.-]+)?$")


class MaterializationError(ValueError):
    """Raised when signed review cannot be materialised safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_timestamp(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MaterializationError("%s is empty" % field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MaterializationError("%s is not ISO-8601" % field) from exc
    if parsed.tzinfo is None:
        raise MaterializationError("%s must include a timezone" % field)
    return text


def _question_projection(dataset: Dataset) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for question in dataset.questions:
        rows.append(
            {
                "id": question["id"],
                "paper_id": question["paper_id"],
                "number": question["number"],
                "type": question["type"],
                "points": int(question["points"]),
                "stem": question.get("stem") or question.get("content"),
                "options": question.get("options", []),
                "answer_slot_count": question.get("answer_slot_count"),
                "asset_dependency": question.get("asset_dependency"),
                "image_path": question.get("image_path"),
                "image_sha256": question.get("image_sha256"),
            }
        )
    return rows


def _answer_projection(dataset: Dataset) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for question in dataset.questions:
        key = dataset.answer_keys[question["id"]]
        rows.append(
            {
                "question_id": question["id"],
                "kind": key.get("kind"),
                "canonical": key.get("canonical"),
                "correct_options": key.get("correct_options", []),
                "slots": key.get("slots", []),
                "custom_validator": key.get("custom_validator"),
                "points": key.get("points"),
            }
        )
    return rows


def _point_projection(data_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in sorted(_read_jsonl(data_dir / "point_audit.jsonl"), key=lambda item: item["item_id"]):
        rows.append(
            {
                "item_id": row["item_id"],
                "question_id": row["question_id"],
                "paper_id": row["paper_id"],
                "kind": row["kind"],
                "current_points": row["current_points"],
                "reviewed_points": row["reviewed_points"],
            }
        )
    return rows


def signed_content_hashes(data_dir: Union[Path, str]) -> Dict[str, Any]:
    """Return semantic hashes of the content covered by human signatures.

    Status and provenance fields are intentionally excluded so a later
    materialisation can mark the review complete without changing the signed
    question, answer, or point content.
    """

    dataset = load_dataset(data_dir, validate=False)
    questions = _question_projection(dataset)
    answers = _answer_projection(dataset)
    points = _point_projection(dataset.data_dir)
    return {
        "question_items": len(questions),
        "answer_items": len(answers),
        "point_items": len(points),
        "questions_sha256": _sha256_json(questions),
        "answers_sha256": _sha256_json(answers),
        "points_sha256": _sha256_json(points),
    }


def _validate_reports(
    owner: Mapping[str, Any],
    independent: Mapping[str, Any],
    *,
    expected_items: int,
) -> None:
    if owner.get("status") != "complete":
        raise MaterializationError(
            "owner review status is %s; only an unambiguous complete signoff can be materialised"
            % owner.get("status")
        )
    if owner.get("proposals"):
        raise MaterializationError(
            "owner review contains proposed answer/point changes; materialise a revised review version instead"
        )
    if int((owner.get("counts") or {}).get("reviewed_items", 0)) != expected_items:
        raise MaterializationError("owner review does not cover all %d items" % expected_items)
    if independent.get("status") != "complete":
        raise MaterializationError(
            "independent review status is %s; resolve disputes/arbitration before materialisation"
            % independent.get("status")
        )
    if independent.get("proposals"):
        raise MaterializationError(
            "independent review contains proposed answer/point changes; start a revision workflow instead"
        )
    counts = independent.get("counts") or {}
    if int(counts.get("answer_items", 0)) != expected_items or int(counts.get("point_items", 0)) != expected_items:
        raise MaterializationError("independent review does not cover all answer and point items")

    owner_person = owner.get("reviewer") if isinstance(owner.get("reviewer"), dict) else {}
    reviewers = independent.get("reviewers") if isinstance(independent.get("reviewers"), dict) else {}
    people = [reviewers.get("1"), reviewers.get("2")]
    if not all(isinstance(person, dict) for person in people):
        raise MaterializationError("independent review must contain two reviewer identities")
    owner_ids = {str(owner_person.get("reviewer_id", "")).strip(), str(owner_person.get("reviewer_name", "")).strip()}
    for person in people:
        ids = {str(person.get("reviewer_id", "")).strip(), str(person.get("reviewer_name", "")).strip()}
        if owner_ids & ids:
            raise MaterializationError("owner and independent reviewers must be three different people")


def _review_event(
    *,
    target_version: str,
    target_type: str,
    item_id: str,
    role: str,
    reviewer_id: str,
    decision: str,
    value: Any,
    canonical_value: Any,
    notes: str,
    reviewed_at: str,
    source_manifest_sha256: str,
    report_sha256: str,
) -> Dict[str, Any]:
    action = {"agree": "accept", "correct": "revise", "dispute": "reject"}.get(decision, decision)
    return {
        "schema_version": "1.0",
        "review_event_id": "rev-%s-%s-%s-%s" % (target_version, role, target_type, item_id),
        "target_type": target_type,
        "target_id": item_id,
        "review_type": "answer_verification" if target_type == "answer" else "point_verification",
        "reviewer_id": reviewer_id,
        "role": role,
        "decision": action,
        "source_decision": decision,
        "value": value,
        "canonical_value": canonical_value,
        "notes": notes,
        "created_at": reviewed_at,
        "source_snapshot": source_manifest_sha256,
        "review_report_sha256": report_sha256,
    }


def _build_review_events(
    owner: Mapping[str, Any],
    independent: Mapping[str, Any],
    *,
    target_version: str,
    source_manifest_sha256: str,
    owner_report_sha256: str,
    independent_report_sha256: str,
) -> List[Dict[str, Any]]:
    owner_person = owner["reviewer"]
    reviewers = independent["reviewers"]
    events: List[Dict[str, Any]] = []
    for item in owner.get("items", []):
        qid = item["item_id"]
        events.append(
            _review_event(
                target_version=target_version,
                target_type="answer",
                item_id=qid,
                role="owner",
                reviewer_id=str(owner_person["reviewer_id"]),
                decision=item["decision"],
                value=item.get("final_answer"),
                canonical_value=item.get("canonical_answer"),
                notes=str(item.get("notes", "")),
                reviewed_at=str(owner_person["reviewed_at"]),
                source_manifest_sha256=source_manifest_sha256,
                report_sha256=owner_report_sha256,
            )
        )
        events.append(
            _review_event(
                target_version=target_version,
                target_type="points",
                item_id=qid,
                role="owner",
                reviewer_id=str(owner_person["reviewer_id"]),
                decision=item["decision"],
                value=item.get("final_points"),
                canonical_value=item.get("canonical_points"),
                notes=str(item.get("notes", "")),
                reviewed_at=str(owner_person["reviewed_at"]),
                source_manifest_sha256=source_manifest_sha256,
                report_sha256=owner_report_sha256,
            )
        )

    for item in independent.get("items", []):
        item_type = str(item["item_type"])
        target_type = "answer" if item_type == "answer" else "points"
        for slot in (1, 2):
            person = reviewers[str(slot)]
            events.append(
                _review_event(
                    target_version=target_version,
                    target_type=target_type,
                    item_id=item["item_id"],
                    role="reviewer_%d" % slot,
                    reviewer_id=str(person["reviewer_id"]),
                    decision=item["decisions"][slot - 1],
                    value=item["reviewer_values"][slot - 1],
                    canonical_value=item["canonical_value"],
                    notes="",
                    reviewed_at=str(person["reviewed_at"]),
                    source_manifest_sha256=source_manifest_sha256,
                    report_sha256=independent_report_sha256,
                )
            )
    return sorted(events, key=lambda row: (row["role"], row["target_type"], row["target_id"]))


def _safe_new_destination(source: Path, destination: Path, target_version: str) -> None:
    if not VERSION_PATTERN.fullmatch(target_version):
        raise MaterializationError("target version is not a supported immutable version string: %s" % target_version)
    if destination.name != target_version:
        raise MaterializationError("output directory name must equal the target dataset version")
    if destination.exists():
        raise MaterializationError("output directory already exists; immutable versions are never overwritten")
    if source == destination or source in destination.parents or destination in source.parents:
        raise MaterializationError("source and destination dataset directories must not overlap")


def _copy_dataset(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".%s-materialize-" % destination.name, dir=destination.parent))
    try:
        for path in source.rglob("*"):
            if path.is_symlink():
                raise MaterializationError("dataset contains a symbolic link and cannot be materialised safely: %s" % path)
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def materialize_signed_dataset(
    source_dir: Union[Path, str],
    output_dir: Union[Path, str],
    *,
    target_version: str,
    created_at: Optional[str] = None,
    owner_review_csv: Union[Path, str],
    owner_signoff: Union[Path, str],
    answers_1: Union[Path, str],
    answers_2: Union[Path, str],
    points_1: Union[Path, str],
    points_2: Union[Path, str],
    arbitration: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """Revalidate signed inputs and create a new immutable reviewed version."""

    source = Path(source_dir).resolve()
    destination = Path(output_dir).resolve()
    source_dataset = load_dataset(source)
    validate_dataset(source_dataset)
    source_version = str(source_dataset.manifest["dataset_version"])
    if target_version == source_version:
        raise MaterializationError("target version must differ from the source version")
    _safe_new_destination(source, destination, target_version)
    created_at = _parse_timestamp(created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "created_at")
    source_manifest_sha = _sha256_file(source / "manifest.json")

    owner = import_owner_signoff(source, signoff=owner_signoff, review_csv=owner_review_csv)
    independent = import_review_packet(
        source,
        answers_1=answers_1,
        answers_2=answers_2,
        points_1=points_1,
        points_2=points_2,
        arbitration=arbitration,
    )
    _validate_reports(owner, independent, expected_items=len(source_dataset.questions))
    if owner.get("dataset_manifest_sha256") != source_manifest_sha:
        raise MaterializationError("owner report is not bound to the source manifest")
    if independent.get("dataset_manifest_sha256") != source_manifest_sha:
        raise MaterializationError("independent report is not bound to the source manifest")

    signed_hashes = signed_content_hashes(source)
    owner_report_sha = _sha256_json(owner)
    independent_report_sha = _sha256_json(independent)
    review_events = _build_review_events(
        owner,
        independent,
        target_version=target_version,
        source_manifest_sha256=source_manifest_sha,
        owner_report_sha256=owner_report_sha,
        independent_report_sha256=independent_report_sha,
    )
    expected_events = len(source_dataset.questions) * 6
    if len(review_events) != expected_events:
        raise MaterializationError(
            "review event count is %d, expected %d" % (len(review_events), expected_events)
        )

    _copy_dataset(source, destination)
    try:
        for doc_name in ("DATA_CARD.md", "REPRODUCE.md", "LICENSE_AND_RIGHTS.md"):
            doc_path = destination / doc_name
            if doc_path.is_file():
                text = doc_path.read_text(encoding="utf-8")
                doc_path.write_text(text.replace(source_version, target_version), encoding="utf-8")

        answer_keys = _read_jsonl(destination / "answer_keys.jsonl")
        for row in answer_keys:
            row["pre_signoff_answer_status"] = row.get("answer_status")
            row["answer_status"] = SIGNED_REVIEW_STATUS
        _write_jsonl(destination / "answer_keys.jsonl", answer_keys)

        answer_audit = _read_jsonl(destination / "answer_audit.jsonl")
        for row in answer_audit:
            row["pre_signoff_status"] = row.get("status")
            row["status"] = SIGNED_REVIEW_STATUS
            row["owner_signoff"] = "complete"
            row["human_independent_review"] = "complete"
            row["review_report_sha256"] = owner_report_sha
        _write_jsonl(destination / "answer_audit.jsonl", answer_audit)

        point_audit = _read_jsonl(destination / "point_audit.jsonl")
        for row in point_audit:
            row["pre_signoff_status"] = row.get("status")
            row["status"] = SIGNED_REVIEW_STATUS
            row["owner_signoff"] = "complete"
            row["human_independent_review"] = "complete"
            row["review_report_sha256"] = owner_report_sha
        _write_jsonl(destination / "point_audit.jsonl", point_audit)

        questions = _read_jsonl(destination / "questions.jsonl")
        for row in questions:
            row["pre_signoff_review_status"] = row.get("review_status")
            row["review_status"] = SIGNED_REVIEW_STATUS
        _write_jsonl(destination / "questions.jsonl", questions)
        _write_jsonl(destination / "reviews.jsonl", review_events)

        if signed_content_hashes(destination) != signed_hashes:
            raise MaterializationError("materialisation changed signed answer, question, or point content")

        owner_person = owner["reviewer"]
        reviewers = independent["reviewers"]
        materialization_doc = """# Review materialisation record

> Source: `{source_version}`  
> Target: `{target_version}`  
> Created at: `{created_at}`  
> Source manifest SHA-256: `{source_manifest_sha}`  
> Signed review status: `{signed_status}`

## Human review

- Owner reviewer ID: `{owner_id}`; reviewed at `{owner_at}`.
- Independent reviewer 1: `{reviewer_1_id}`; reviewed at `{reviewer_1_at}`.
- Independent reviewer 2: `{reviewer_2_id}`; reviewed at `{reviewer_2_at}`.
- Owner report SHA-256: `{owner_report_sha}`.
- Independent report SHA-256: `{independent_report_sha}`.
- Review events written: `{event_count}`.

No answer or point changes were proposed.  The materialiser verified the
semantic hashes of question, answer-key, and point content before and after
adding the signoff state.  Raw signatures remain in the controlled review
evidence; this public-facing data package stores reviewer IDs and hashes, not
replayed signatures.

This is a human-reviewed alternative protocol, not an exam-authority
certification.  `official_answer_source_found` remains `false`.
""".format(
            source_version=source_version,
            target_version=target_version,
            created_at=created_at,
            source_manifest_sha=source_manifest_sha,
            signed_status=SIGNED_REVIEW_STATUS,
            owner_id=owner_person.get("reviewer_id"),
            owner_at=owner_person.get("reviewed_at"),
            reviewer_1_id=reviewers["1"].get("reviewer_id"),
            reviewer_1_at=reviewers["1"].get("reviewed_at"),
            reviewer_2_id=reviewers["2"].get("reviewer_id"),
            reviewer_2_at=reviewers["2"].get("reviewed_at"),
            owner_report_sha=owner_report_sha,
            independent_report_sha=independent_report_sha,
            event_count=len(review_events),
        )
        (destination / MATERIALIZATION_DOCUMENT).write_text(materialization_doc, encoding="utf-8")

        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        manifest["dataset_version"] = target_version
        manifest["created_at"] = created_at
        manifest["supersedes"] = source_version
        manifest["status"] = "human_review_materialized_candidate"
        manifest["public_release"] = False
        manifest["counts"]["review_records"] = len(review_events)
        manifest["audit"]["answer_statuses"] = {SIGNED_REVIEW_STATUS: len(answer_audit)}
        manifest["audit"]["point_statuses"] = {SIGNED_REVIEW_STATUS: len(point_audit)}
        manifest["audit"]["pre_signoff_answer_statuses"] = sorted(
            {str(row.get("pre_signoff_status")) for row in answer_audit}
        )
        manifest["audit"]["pre_signoff_point_statuses"] = sorted(
            {str(row.get("pre_signoff_status")) for row in point_audit}
        )
        manifest["quality"].update(
            {
                "owner_signoff": "complete",
                "human_independent_review": "complete",
                "independent_review": "complete",
                "answer_status": SIGNED_REVIEW_STATUS,
                "point_status": SIGNED_REVIEW_STATUS,
                "answer_evidence": "owner_and_two_independent_reviewers_signed_media_crosscheck",
                "review_materialization": "complete",
            }
        )
        manifest["review_lineage"] = {
            "schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "source_dataset_version": source_version,
            "source_manifest_sha256": source_manifest_sha,
            "created_at": created_at,
            "owner_report_sha256": owner_report_sha,
            "independent_report_sha256": independent_report_sha,
            "signed_content": signed_hashes,
            "review_event_count": len(review_events),
            "materialization_document": MATERIALIZATION_DOCUMENT,
        }
        manifest["files"][MATERIALIZATION_DOCUMENT] = {
            "bytes": (destination / MATERIALIZATION_DOCUMENT).stat().st_size,
            "sha256": _sha256_file(destination / MATERIALIZATION_DOCUMENT),
        }
        for relative in sorted(manifest["files"]):
            path = destination / relative
            if not path.is_file():
                raise MaterializationError("manifest file is missing after materialisation: %s" % relative)
            manifest["files"][relative] = {"bytes": path.stat().st_size, "sha256": _sha256_file(path)}
        _write_json(destination / "manifest.json", manifest)

        reviewed_dataset = load_dataset(destination)
        audit = validate_dataset(reviewed_dataset)
        if signed_content_hashes(destination) != signed_hashes:
            raise MaterializationError("post-manifest signed-content verification failed")
        smoke_cases = {}
        for case_name, expected_points in (("gold", 353), ("empty", 0), ("malformed", 0)):
            submission = json.loads(
                (destination / "examples" / (case_name + ".json")).read_text(encoding="utf-8")
            )
            report = evaluate_submission(reviewed_dataset, submission["answers"])
            if report["summary"]["earned_points"] != expected_points:
                raise MaterializationError(
                    "materialised dataset no longer scores %s at %d/%d"
                    % (case_name, expected_points, expected_points)
                )
            smoke_cases[case_name] = {
                "expected_points": expected_points,
                "earned_points": report["summary"]["earned_points"],
                "GB-2025-Math-Obj": report["summary"]["GB-2025-Math-Obj"],
                "failure_counts": report["summary"]["failure_counts"],
            }
        gold_report = evaluate_submission(
            reviewed_dataset,
            json.loads((destination / "examples" / "gold.json").read_text(encoding="utf-8"))["answers"],
        )
        if gold_report["summary"]["earned_points"] != 353 or gold_report["summary"]["correct_questions"] != 74:
            raise MaterializationError("materialised dataset no longer scores gold at 353/353")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise

    output_manifest_sha = _sha256_file(destination / "manifest.json")
    smoke = {
        "dataset": "Gaokao-Bench",
        "dataset_version": target_version,
        "dataset_manifest_sha256": output_manifest_sha,
        "scorer_version": SCORER_VERSION,
        "audit": audit,
        "smoke_cases": smoke_cases,
        "status": "pass",
    }
    return {
        "source_version": source_version,
        "target_version": target_version,
        "output_dir": str(destination),
        "source_manifest_sha256": source_manifest_sha,
        "output_manifest_sha256": output_manifest_sha,
        "owner_report_sha256": owner_report_sha,
        "independent_report_sha256": independent_report_sha,
        "review_events": len(review_events),
        "signed_content": signed_hashes,
        "gold": {
            "earned_points": gold_report["summary"]["earned_points"],
            "possible_points": gold_report["summary"]["possible_points"],
            "correct_questions": gold_report["summary"]["correct_questions"],
        },
        "smoke": smoke,
        "status": "complete",
    }


def write_materialization_report(report: Mapping[str, Any], path: Union[Path, str]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, dict(report))
    return output


def write_smoke_report(report: Mapping[str, Any], path: Union[Path, str]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, report["smoke"])
    return output


def write_materialized_release_evidence(
    source_data_dir: Union[Path, str],
    output_data_dir: Union[Path, str],
    materialization: Mapping[str, Any],
    *,
    release_root: Union[Path, str] = "release",
) -> Dict[str, Any]:
    """Copy unsigned release templates and re-bind them to the new manifest.

    This never creates ``rights-attestation.json`` or ``answer-protocol.json``;
    those still require the owner's real signature and explicit decision.
    """

    source = Path(source_data_dir).resolve()
    output = Path(output_data_dir).resolve()
    project_root = source.parent.parent if source.parent.name == "data" else source.parent
    root = Path(release_root)
    if not root.is_absolute():
        root = project_root / root
    source_version = str(materialization["source_version"])
    target_version = str(materialization["target_version"])
    source_evidence = project_root / "release" / source_version
    destination = root / target_version
    if not source_evidence.is_dir():
        raise MaterializationError("source release evidence directory is missing: %s" % source_evidence)
    if destination.exists():
        raise MaterializationError("release evidence directory already exists: %s" % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".%s-release-" % target_version, dir=destination.parent))
    try:
        for name in ("release-policy.json", "rights-attestation.template.json", "answer-protocol.template.json", "README.md"):
            source_file = source_evidence / name
            if source_file.is_file():
                shutil.copy2(source_file, temporary / name)
        for name in ("release-policy.json", "rights-attestation.template.json", "answer-protocol.template.json"):
            path = temporary / name
            if not path.is_file():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["dataset_version"] = target_version
            payload["dataset_manifest_sha256"] = materialization["output_manifest_sha256"]
            if name == "release-policy.json":
                full_public = payload.get("full_public_data_release")
                if isinstance(full_public, dict):
                    full_public["rights_attestation"] = "release/%s/rights-attestation.json" % target_version
                answer = payload.get("answer_validation")
                if isinstance(answer, dict):
                    answer["answer_protocol"] = "release/%s/answer-protocol.json" % target_version
            else:
                payload["owner_attested"] = False
                if name == "rights-attestation.template.json":
                    payload["legal_clearance_claimed"] = False
                for field in ("reviewer_id", "reviewer_name", "signature", "reviewed_at"):
                    payload[field] = ""
            _write_json(path, payload)
        readme = temporary / "README.md"
        if readme.is_file():
            readme.write_text(readme.read_text(encoding="utf-8").replace(source_version, target_version), encoding="utf-8")
        evidence = {
            "schema_version": MATERIALIZATION_SCHEMA_VERSION,
            "source_dataset_version": source_version,
            "target_dataset_version": target_version,
            "target_dataset_manifest_sha256": materialization["output_manifest_sha256"],
            "owner_report_sha256": materialization["owner_report_sha256"],
            "independent_report_sha256": materialization["independent_report_sha256"],
            "signed_content": materialization["signed_content"],
            "signed_files_created": [],
            "unsigned_templates": [
                "rights-attestation.template.json",
                "answer-protocol.template.json",
            ],
        }
        _write_json(temporary / "materialization-evidence.json", evidence)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {
        "release_evidence_dir": str(destination),
        "release_policy": str(destination / "release-policy.json"),
        "rights_template": str(destination / "rights-attestation.template.json"),
        "answer_protocol_template": str(destination / "answer-protocol.template.json"),
        "materialization_evidence": str(destination / "materialization-evidence.json"),
        "signed_files_created": [],
    }
