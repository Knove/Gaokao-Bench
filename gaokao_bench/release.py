"""Executable release gates for Gaokao-Bench.

The gate does not sign on behalf of a human.  It verifies that the artifacts
produced by the existing review tools are present, bind to the current dataset
manifest, and are sufficient for the requested release target.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .harness import (
    HARNESS_VERSION,
    prepare_prompt,
)
from .benchmark_spec import question_requires_vision
from .materialize import signed_content_hashes
from .scoring import SCORER_VERSION, file_sha256, load_dataset, validate_dataset
from .version import DEFAULT_DATASET_VERSION


RELEASE_TARGETS = ("internal", "public-code", "public")
PUBLIC_RIGHTS_VALUES = {"cleared", "public", "cleared_with_attribution"}
PUBLIC_CODE_SCOPE = "code_and_project_original_documentation"
PROVISIONAL_SIGNOFF_MARKERS = (
    "pending",
    "codex",
    "agent",
    "provisional",
    "automatic",
    "automated",
    "tbd",
    "todo",
)


def _read_json(path: Optional[Path | str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    value = json.loads(candidate.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _add(checks: List[Dict[str, Any]], name: str, status: str, detail: str, *, public_blocking: bool = False) -> None:
    checks.append(
        {
            "name": name,
            "status": status,
            "detail": detail,
            "public_blocking": public_blocking,
        }
    )


def _git_state(repo_root: Path) -> Dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"available": False, "head": None, "dirty": True, "error": str(exc)}
    return {
        "available": head.returncode == 0,
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()),
        "status": status.stdout.strip(),
    }


def _default_release_evidence_path(data_dir: Path, filename: str) -> Path:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest.get("dataset_version", data_dir.name))
    project_root = data_dir.parent.parent if data_dir.parent.name == "data" else data_dir.parent
    return project_root / "release" / version / filename


def _evidence_path(
    data_dir: Path,
    manifest: Mapping[str, Any],
    filename: str,
) -> Path:
    """Resolve a ledger in either the source or Hugging Face public layout."""

    audit = manifest.get("audit") if isinstance(manifest.get("audit"), Mapping) else {}
    declared = audit.get("files") if isinstance(audit.get("files"), Mapping) else {}
    candidates: List[Path] = []
    for value in declared.values():
        if not isinstance(value, str):
            continue
        candidate = data_dir / value
        if candidate.name == filename:
            candidates.append(candidate)
    candidates.extend((data_dir / filename, data_dir / "audit" / filename))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def resolve_release_report_paths(data_dir: Path | str, *, repo_root: Path | str = ".") -> Dict[str, Path]:
    """Resolve report defaults from the dataset and its materialisation lineage.

    Owner and independent-review reports belong to the source version whose
    signatures were materialised.  Smoke and gate reports belong to the current
    immutable version.  Keeping this derivation in one place prevents a newly
    materialised version from silently reusing the previous version's evidence.
    """

    root = Path(data_dir).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    dataset_version = str(manifest.get("dataset_version", root.name))
    lineage = manifest.get("review_lineage") if isinstance(manifest.get("review_lineage"), dict) else {}
    review_source_version = str(lineage.get("source_dataset_version", dataset_version))
    reports = Path(repo_root).resolve() / "reports"
    return {
        "owner_report": reports / ("%s-owner-signoff.json" % review_source_version),
        "independent_report": reports / ("%s-independent-review-import.json" % review_source_version),
        "smoke_report": reports / ("%s-smoke.json" % dataset_version),
    }


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _parse_signed_timestamp(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _signed_identity_errors(payload: Dict[str, Any], location: str) -> List[str]:
    errors: List[str] = []
    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    reviewer_name = str(payload.get("reviewer_name", "")).strip()
    signature = str(payload.get("signature", "")).strip()
    if not reviewer_id or any(marker in reviewer_id.lower() for marker in PROVISIONAL_SIGNOFF_MARKERS):
        errors.append("%s reviewer_id is empty or provisional" % location)
    if not reviewer_name or any(marker in reviewer_name.lower() for marker in PROVISIONAL_SIGNOFF_MARKERS):
        errors.append("%s reviewer_name is empty or provisional" % location)
    if not signature or signature != reviewer_name:
        errors.append("%s signature must exactly equal reviewer_name" % location)
    if not _parse_signed_timestamp(payload.get("reviewed_at")):
        errors.append("%s reviewed_at must be ISO-8601 with a timezone" % location)
    return errors


def _review_check(
    report_path: Optional[Path | str],
    *,
    manifest_sha: str,
    expected_items: int,
    kind: str,
    public_blocking: bool,
    lineage: Optional[Dict[str, Any]] = None,
    current_signed_content: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    report = _read_json(report_path)
    if report is None:
        return {
            "status": "fail",
            "detail": "%s report not found; run the corresponding import command" % kind,
            "public_blocking": public_blocking,
        }
    report_manifest_sha = report.get("dataset_manifest_sha256")
    binding_manifest_sha = manifest_sha
    binding_mode = "current"
    if report_manifest_sha != manifest_sha:
        lineage = lineage if isinstance(lineage, dict) else {}
        expected_source = str(lineage.get("source_manifest_sha256", ""))
        expected_report_hash = str(lineage.get("%s_report_sha256" % kind, ""))
        actual_report_hash = file_sha256(Path(report_path))
        lineage_errors: List[str] = []
        if lineage.get("schema_version") != "1.0":
            lineage_errors.append("dataset has no valid review materialisation lineage")
        if report_manifest_sha != expected_source:
            lineage_errors.append("report is bound to neither the current manifest nor the materialisation source")
        if not expected_report_hash or actual_report_hash != expected_report_hash:
            lineage_errors.append("report SHA-256 does not match the materialisation lineage")
        if current_signed_content is None or lineage.get("signed_content") != current_signed_content:
            lineage_errors.append("current signed answer/question/point content no longer matches the materialised lineage")
        if lineage_errors:
            return {
                "status": "fail",
                "detail": "%s; report manifest=%s current manifest=%s" % (
                    "; ".join(lineage_errors),
                    report_manifest_sha,
                    manifest_sha,
                ),
                "public_blocking": public_blocking,
            }
        binding_manifest_sha = expected_source
        binding_mode = "materialized_lineage"
    if report.get("dataset_manifest_sha256") != binding_manifest_sha:
        return {
            "status": "fail",
            "detail": "%s report is bound to a different dataset manifest" % kind,
            "public_blocking": public_blocking,
        }
    if report.get("dataset_mutated") is not False:
        return {
            "status": "fail",
            "detail": "%s report does not prove a proposal-only import" % kind,
            "public_blocking": public_blocking,
        }
    if report.get("status") != "complete":
        return {
            "status": "fail",
            "detail": "%s status is %s; disputes, proposals, or missing items must be resolved and versioned" % (kind, report.get("status")),
            "public_blocking": public_blocking,
        }
    if report.get("proposals"):
        return {
            "status": "fail",
            "detail": "%s contains proposed changes; publish a new immutable dataset version before release" % kind,
            "public_blocking": public_blocking,
        }
    counts = report.get("counts") or {}
    reviewed = int(counts.get("reviewed_items", 0))
    if reviewed < expected_items:
        return {
            "status": "fail",
            "detail": "%s reviewed %d/%d items" % (kind, reviewed, expected_items),
            "public_blocking": public_blocking,
        }
    if kind == "owner" and report.get("owner_signoff") != "complete":
        return {
            "status": "fail",
            "detail": "owner signoff is not complete",
            "public_blocking": public_blocking,
        }
    if kind == "owner":
        reviewer = report.get("reviewer") if isinstance(report.get("reviewer"), dict) else {}
        identity_errors = _signed_identity_errors(reviewer, "owner report")
        if identity_errors:
            return {
                "status": "fail",
                "detail": "; ".join(identity_errors),
                "public_blocking": public_blocking,
            }
        input_hashes = report.get("input_sha256") if isinstance(report.get("input_sha256"), dict) else {}
        if input_hashes.get("manifest") != binding_manifest_sha or not _valid_sha256(input_hashes.get("owner_review.csv")):
            return {
                "status": "fail",
                "detail": "owner report is missing manifest/worksheet input hashes",
                "public_blocking": public_blocking,
            }
    if kind == "independent" and report.get("human_independent_review") != "complete":
        return {
            "status": "fail",
            "detail": "independent review is not complete",
            "public_blocking": public_blocking,
        }
    if kind == "independent":
        reviewers = report.get("reviewers") if isinstance(report.get("reviewers"), dict) else {}
        metadata = [reviewers.get(slot) for slot in ("1", "2")]
        if not all(isinstance(row, dict) for row in metadata):
            return {
                "status": "fail",
                "detail": "independent report does not contain two reviewer identities",
                "public_blocking": public_blocking,
            }
        errors: List[str] = []
        for index, reviewer in enumerate(metadata, 1):
            errors.extend(_signed_identity_errors(dict(reviewer), "reviewer-%d" % index))
        if metadata[0].get("reviewer_id") == metadata[1].get("reviewer_id") or metadata[0].get("reviewer_name") == metadata[1].get("reviewer_name"):
            errors.append("independent reviewers must be two different people")
        input_hashes = report.get("input_sha256") if isinstance(report.get("input_sha256"), dict) else {}
        required_hashes = ("answers.reviewer-1", "answers.reviewer-2", "points.reviewer-1", "points.reviewer-2")
        if any(not _valid_sha256(input_hashes.get(name)) for name in required_hashes):
            errors.append("independent report is missing reviewer file hashes")
        if errors:
            return {
                "status": "fail",
                "detail": "; ".join(errors),
                "public_blocking": public_blocking,
            }
    return {
        "status": "pass",
        "detail": "%s report is complete and bound to the %s manifest" % (kind, "source" if binding_mode == "materialized_lineage" else "current"),
        "public_blocking": public_blocking,
        "binding_mode": binding_mode,
    }


def _prompt_crop_check(data_dir: Path) -> Dict[str, Any]:
    dataset = load_dataset(data_dir)
    required = {
        question["id"]: question
        for question in dataset.questions
        if question_requires_vision(question)
    }
    crops = dataset.spec.prompt.image_crops
    hashes = dataset.spec.prompt.image_pixel_sha256
    if set(required) != set(crops) or set(required) != set(hashes):
        return {"status": "fail", "detail": "prompt crop registry does not match required-vision questions"}
    for qid, question in required.items():
        _, image = prepare_prompt(question, data_dir, "vision", prompt_spec=dataset.spec.prompt)
        if image is None or image.pixel_sha256 != hashes[qid]:
            return {"status": "fail", "detail": "prompt crop pixel hash mismatch for %s" % qid}
    return {
        "status": "pass",
        "detail": "%d/%d required-vision prompt crops render and match reviewed pixel hashes"
        % (len(required), len(required)),
    }


def _owner_attestation_check(
    data_dir: Path,
    manifest: Dict[str, Any],
    *,
    expected_items: int,
    expected_points: int,
) -> Dict[str, Any]:
    """Verify the in-package single-owner attestation.

    A single-owner protocol has no separate independent-review report.  The
    gate must not invent one or leave a permanent ``pending`` state.  Instead,
    it verifies the immutable attestation, the review protocol, the review
    events, and every input hash committed by the owner.
    """

    signoff_path = _evidence_path(data_dir, manifest, "owner_signoff.json")
    protocol_path = _evidence_path(data_dir, manifest, "review_protocol.json")
    if not signoff_path.is_file() or not protocol_path.is_file():
        return {
            "status": "fail",
            "detail": "owner-attested dataset is missing owner_signoff.json or review_protocol.json",
        }
    signoff = _read_json(signoff_path) or {}
    protocol = _read_json(protocol_path) or {}
    dataset_version = str(manifest.get("dataset_version", ""))
    errors: List[str] = []
    if signoff.get("dataset_version") != dataset_version:
        errors.append("owner_signoff is bound to a different dataset version")
    if signoff.get("status") not in {"audited_passed", "complete", "passed"}:
        errors.append("owner_signoff status is not audited-passed")
    if signoff.get("decision") != "accept_all" or signoff.get("owner_attested") is not True:
        errors.append("owner_signoff does not record an accept-all owner attestation")
    reviewer = signoff.get("reviewer") if isinstance(signoff.get("reviewer"), dict) else {}
    errors.extend(_signed_identity_errors(reviewer, "owner_signoff"))
    counts = signoff.get("counts") if isinstance(signoff.get("counts"), dict) else {}
    if int(counts.get("questions", -1)) != expected_items:
        errors.append("owner_signoff question count is stale")
    if int(counts.get("points", -1)) != expected_points:
        errors.append("owner_signoff point total is stale")
    for name, digest in (signoff.get("input_sha256") or {}).items():
        path = data_dir / str(name)
        if not path.is_file():
            path = _evidence_path(data_dir, manifest, Path(str(name)).name)
        if not path.is_file():
            errors.append("owner_signoff input is missing: %s" % name)
        elif digest != file_sha256(path):
            errors.append("owner_signoff input hash is stale: %s" % name)
    if protocol.get("protocol") != "owner_attested_single_review":
        errors.append("review protocol is not owner_attested_single_review")
    if protocol.get("status") not in {"audited_passed", "complete", "passed"}:
        errors.append("review protocol status is not audited-passed")
    if protocol.get("owner_signoff") not in {"audited_passed", "complete", "passed"}:
        errors.append("review protocol owner_signoff is not audited-passed")
    if protocol.get("independent_review") not in {
        "not_required_single_owner_attestation",
        "not_applicable_single_owner_attestation",
    }:
        errors.append("single-owner protocol must explicitly mark independent review as not applicable")
    if int(protocol.get("review_event_count", -1)) != expected_items:
        errors.append("review protocol event count is stale")
    reviews = _read_jsonl(_evidence_path(data_dir, manifest, "reviews.jsonl"))
    if len(reviews) != expected_items:
        errors.append("review events cover %d/%d questions" % (len(reviews), expected_items))
    for row in reviews:
        if row.get("review_protocol", row.get("protocol")) not in (None, "owner_attested_single_review"):
            errors.append("review event uses a different protocol: %s" % row.get("review_event_id"))
            break
        if row.get("status") not in {"audited_passed", "complete", "passed"}:
            errors.append("review event is not audited-passed: %s" % row.get("review_event_id"))
            break
    if errors:
        return {"status": "fail", "detail": "; ".join(errors)}
    return {
        "status": "pass",
        "detail": (
            "single-owner attestation is complete; %d review events and all input hashes verified"
            % len(reviews)
        ),
    }


def _smoke_check(
    report_path: Optional[Path | str],
    *,
    manifest_sha: str,
    dataset_version: str,
    expected_items: int,
    possible_points: int,
) -> Dict[str, Any]:
    report = _read_json(report_path)
    if report is None:
        return {"status": "fail", "detail": "smoke report is missing: %s" % report_path}
    if report.get("dataset_manifest_sha256") != manifest_sha:
        return {"status": "fail", "detail": "smoke report is not bound to the current dataset manifest"}
    if report.get("dataset_version") != dataset_version:
        return {"status": "fail", "detail": "smoke report is bound to a different dataset version"}
    if report.get("scorer_version") != SCORER_VERSION:
        return {"status": "fail", "detail": "smoke report scorer_version does not match the current scorer"}
    audit = report.get("audit") if isinstance(report.get("audit"), dict) else {}
    if audit.get("dataset_version") != dataset_version or audit.get("questions") != expected_items:
        return {"status": "fail", "detail": "smoke audit does not cover the current %d-question dataset" % expected_items}
    if report.get("status") != "pass":
        return {"status": "fail", "detail": "smoke report status is %s" % report.get("status")}
    cases = report.get("smoke_cases") if isinstance(report.get("smoke_cases"), dict) else {}
    expected_points = {"gold": possible_points, "empty": 0, "malformed": 0}
    for name, points in expected_points.items():
        case = cases.get(name) if isinstance(cases.get(name), dict) else {}
        if case.get("expected_points") != points or case.get("earned_points") != points:
            return {
                "status": "fail",
                "detail": "smoke case %s must deterministically score %d/%d points" % (name, points, points),
            }
    return {"status": "pass", "detail": "gold/empty/malformed smoke cases passed and are bound to the dataset manifest"}


def _clean_run_check(run_summary: Optional[Dict[str, Any]], manifest_sha: str) -> Dict[str, Any]:
    if run_summary is None:
        return {"status": "fail", "detail": "no clean model run summary supplied"}
    if run_summary.get("dataset_manifest_sha256") != manifest_sha:
        return {"status": "fail", "detail": "run summary is bound to a different dataset manifest"}
    if run_summary.get("harness_version") != HARNESS_VERSION:
        return {"status": "fail", "detail": "run summary harness_version does not match the current harness"}
    if run_summary.get("scorer_version") != SCORER_VERSION:
        return {"status": "fail", "detail": "run summary scorer_version does not match the current scorer"}
    if run_summary.get("modality") != "vision" or int(run_summary.get("repeats", 0)) < 3:
        return {"status": "fail", "detail": "clean run must be vision profile with at least 3 repeats"}
    if run_summary.get("provider") == "mock" or run_summary.get("warning"):
        return {"status": "fail", "detail": "mock or warned run cannot be a release score"}
    attempts = run_summary.get("attempts") or {}
    if int(attempts.get("harness_errors", 0)) or int(attempts.get("provider_errors", 0)):
        return {"status": "fail", "detail": "clean run has harness/provider errors; keep it in the audit trail but do not release it"}
    return {"status": "pass", "detail": "clean vision run is bound to dataset, harness, and scorer versions"}


def _read_release_policy(data_dir: Path, policy_path: Optional[Path | str]) -> tuple[Optional[Dict[str, Any]], Path]:
    path = Path(policy_path) if policy_path else _default_release_evidence_path(data_dir, "release-policy.json")
    payload = _read_json(path)
    return payload, path


def _contamination_check(
    policy: Optional[Dict[str, Any]],
    *,
    manifest_sha: str,
    dataset_version: str,
    source_root: Path,
    dataset_years: Sequence[int],
) -> Dict[str, Any]:
    if policy is None:
        return {"status": "fail", "detail": "release policy with contamination disclosure is missing"}
    if policy.get("dataset_manifest_sha256") != manifest_sha or policy.get("dataset_version") != dataset_version:
        return {"status": "fail", "detail": "contamination disclosure is bound to a different dataset"}
    disclosure = policy.get("contamination_disclosure") if isinstance(policy.get("contamination_disclosure"), dict) else {}
    accepted_statuses = {"public_%s_contaminated_disclosed" % "-".join(str(year) for year in dataset_years)}
    accepted_statuses.add("public_2025_contaminated_disclosed")
    if disclosure.get("status") not in accepted_statuses:
        return {
            "status": "fail",
            "detail": "public %s contamination is not explicitly disclosed"
            % "/".join(str(year) for year in dataset_years),
        }
    if disclosure.get("freshness_claim_allowed") is not False:
        return {"status": "fail", "detail": "contaminated public questions must not allow a freshness claim"}
    label = str(disclosure.get("score_label", "")).strip().lower()
    if not label or "fresh" in label or "unseen" in label or "heldout" in label.replace("_", ""):
        return {"status": "fail", "detail": "score label must identify a retrospective public-question run"}
    vectors = disclosure.get("known_vectors")
    if not isinstance(vectors, list) or not vectors:
        return {"status": "fail", "detail": "contamination disclosure has no known leakage vectors"}
    document = str(disclosure.get("disclosure_document", "")).strip()
    if not document or not (source_root / document).is_file():
        return {"status": "fail", "detail": "contamination disclosure document is missing"}
    return {
        "status": "pass",
        "detail": "public %s leakage is disclosed; freshness claims are disabled and score_label=%s"
        % ("/".join(str(year) for year in dataset_years), disclosure.get("score_label")),
    }


def _public_code_rights_check(policy: Optional[Dict[str, Any]], source_root: Path) -> Dict[str, Any]:
    if policy is None:
        return {"status": "fail", "detail": "release policy is missing"}
    scope = policy.get("public_code_release") if isinstance(policy.get("public_code_release"), dict) else {}
    expected = {
        "status": "allowed_after_clean_revision",
        "artifact_scope": PUBLIC_CODE_SCOPE,
        "question_data_included": False,
        "assets_included": False,
        "third_party_evidence_included": False,
        "internal_data_builders_included": False,
        "artifact_allowlist_document": "SCOPE.md",
    }
    mismatches = [key for key, value in expected.items() if scope.get(key) != value]
    if mismatches:
        return {
            "status": "fail",
            "detail": "public-code scope is not restricted to code/documentation: %s" % ", ".join(mismatches),
        }
    for name in ("LICENSE", "LICENSE-DATA", "NOTICE"):
        if not (source_root / name).is_file():
            return {"status": "fail", "detail": "public-code release is missing %s" % name}
    allowlist_document = source_root / "SCOPE.md"
    if not allowlist_document.is_file():
        return {"status": "fail", "detail": "public-code allowlist document is missing"}
    return {
        "status": "pass",
        "detail": "public-code scope excludes question data, assets, third-party evidence, and internal data builders",
    }


def _full_public_rights_check(
    manifest: Dict[str, Any],
    policy: Optional[Dict[str, Any]],
    *,
    manifest_sha: str,
    dataset_version: str,
    attestation_path: Optional[Path | str],
    source_root: Path,
) -> Dict[str, Any]:
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    if rights.get("public_redistribution") in PUBLIC_RIGHTS_VALUES:
        return {"status": "pass", "detail": "public redistribution is recorded as cleared"}

    path = Path(attestation_path) if attestation_path else _default_release_evidence_path(source_root / "data" / dataset_version, "rights-attestation.json")
    attestation = _read_json(path)
    if attestation is None:
        return {
            "status": "fail",
            "detail": "public redistribution is %s; a signed owner risk attestation is required" % rights.get("public_redistribution"),
        }
    errors: List[str] = []
    if attestation.get("dataset_manifest_sha256") != manifest_sha or attestation.get("dataset_version") != dataset_version:
        errors.append("rights attestation is bound to a different dataset")
    if attestation.get("scope") != "full_public_data_and_assets":
        errors.append("rights attestation scope must be full_public_data_and_assets")
    if attestation.get("decision") != "accept_public_risk":
        errors.append("rights attestation decision must be accept_public_risk")
    if attestation.get("owner_attested") is not True:
        errors.append("owner_attested must be true")
    if attestation.get("legal_clearance_claimed") is not False:
        errors.append("risk acceptance must not be represented as legal clearance")
    errors.extend(_signed_identity_errors(attestation, "rights attestation"))
    if policy is None:
        errors.append("release policy is missing")
    elif policy.get("dataset_manifest_sha256") != manifest_sha:
        errors.append("release policy is bound to a different dataset")
    else:
        policy_release = policy.get("full_public_data_release") if isinstance(policy.get("full_public_data_release"), dict) else {}
        if policy_release.get("status") not in ("requires_signed_owner_attestation", "cleared"):
            errors.append("release policy does not allow a full public data release")
        if policy_release.get("accepted_decision") != "signed_owner_risk_acceptance":
            errors.append("release policy does not accept a signed owner risk decision")
    if errors:
        return {"status": "fail", "detail": "; ".join(errors)}
    return {
        "status": "pass",
        "detail": "owner accepted public redistribution risk; legal clearance is not claimed",
        "warning": "rights_risk_accepted_not_legally_cleared",
    }


def _answer_protocol_check(
    manifest: Dict[str, Any],
    *,
    policy: Optional[Dict[str, Any]],
    manifest_sha: str,
    dataset_version: str,
    independent_pass: bool,
    owner_attested: bool = False,
    protocol_path: Optional[Path | str],
    source_root: Path,
) -> Dict[str, Any]:
    if bool((manifest.get("quality") or {}).get("official_answer_source_found")):
        return {"status": "pass", "detail": "official answer source is recorded"}
    if owner_attested:
        quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
        if quality.get("answer_status") not in {"audited_passed", "complete", "passed"}:
            return {
                "status": "fail",
                "detail": "no official answer source and owner-attested answer audit is not complete",
            }
        if quality.get("review_protocol") != "owner_attested_single_review":
            return {
                "status": "fail",
                "detail": "owner-attested answer protocol is not declared by the manifest",
            }
        return {
            "status": "pass",
            "detail": (
                "no official answer source; the single-owner attestation covers every canonical "
                "answer and explicitly states that independent review is not claimed"
            ),
            "warning": "official_answer_source_absent",
        }
    if not independent_pass:
        return {"status": "fail", "detail": "official answer source is absent and independent review is not complete"}
    path = Path(protocol_path) if protocol_path else _default_release_evidence_path(source_root / "data" / dataset_version, "answer-protocol.json")
    protocol = _read_json(path)
    if protocol is None:
        return {"status": "fail", "detail": "official answer source is absent; a signed alternative validation protocol is required"}
    errors: List[str] = []
    policy_answer = policy.get("answer_validation") if policy and isinstance(policy.get("answer_validation"), dict) else {}
    if policy_answer.get("status") not in ("requires_signed_owner_attestation", "cleared"):
        errors.append("release policy does not allow the alternative answer-validation protocol")
    if policy_answer.get("protocol") != "two_independent_reviewers_plus_owner_signoff":
        errors.append("release policy does not require two independent reviewers plus owner signoff")
    if protocol.get("dataset_manifest_sha256") != manifest_sha or protocol.get("dataset_version") != dataset_version:
        errors.append("answer protocol is bound to a different dataset")
    if protocol.get("protocol") != "two_independent_reviewers_plus_owner_signoff":
        errors.append("answer protocol must require two independent reviewers plus owner signoff")
    if protocol.get("official_answer_source_found") is not False:
        errors.append("answer protocol must state that no official answer source was found")
    if protocol.get("decision") != "accept_alternative_protocol":
        errors.append("answer protocol decision must be accept_alternative_protocol")
    if protocol.get("owner_attested") is not True:
        errors.append("owner_attested must be true")
    errors.extend(_signed_identity_errors(protocol, "answer protocol"))
    if errors:
        return {"status": "fail", "detail": "; ".join(errors)}
    return {
        "status": "pass",
        "detail": "no official answer source; owner-approved two-reviewer alternative validation protocol is recorded",
        "warning": "official_answer_source_absent",
    }


def evaluate_release_gate(
    data_dir: Path | str = "data/%s" % DEFAULT_DATASET_VERSION,
    *,
    target: str = "internal",
    owner_report: Optional[Path | str] = None,
    independent_report: Optional[Path | str] = None,
    smoke_report: Optional[Path | str] = None,
    run_summary: Optional[Path | str] = None,
    release_policy: Optional[Path | str] = None,
    rights_attestation: Optional[Path | str] = None,
    answer_protocol: Optional[Path | str] = None,
    repo_root: Path | str = ".",
) -> Dict[str, Any]:
    """Evaluate a release target without modifying data or signing anything."""

    if target not in RELEASE_TARGETS:
        raise ValueError("unsupported release target: %s" % target)
    root = Path(data_dir).resolve()
    dataset = load_dataset(root, validate=target != "public-code")
    if target != "public-code":
        validate_dataset(dataset)
    manifest = dataset.manifest
    manifest_sha = file_sha256(root / "manifest.json")
    source_root = root.parent.parent if root.parent.name == "data" else root.parent
    public_full = target == "public"
    public_code = target == "public-code"
    public_any = public_full or public_code
    dataset_version = str(manifest.get("dataset_version"))
    review_lineage = manifest.get("review_lineage") if isinstance(manifest.get("review_lineage"), dict) else None
    default_reports = resolve_release_report_paths(root, repo_root=repo_root)
    current_signed_content = signed_content_hashes(root) if review_lineage else None
    checks: List[Dict[str, Any]] = []
    policy, policy_path = _read_release_policy(root, release_policy)
    policy_bound = bool(
        policy
        and policy.get("dataset_manifest_sha256") == manifest_sha
        and policy.get("dataset_version") == dataset_version
    )
    if not policy_bound and public_any:
        _add(
            checks,
            "release_policy",
            "fail",
            "release policy is missing or bound to a different dataset: %s" % policy_path,
            public_blocking=True,
        )
    elif policy_bound:
        _add(checks, "release_policy", "pass", "release policy is bound to the current manifest", public_blocking=False)

    if public_code:
        ids = {question["id"] for question in dataset.questions}
        points = sum(int(question["points"]) for question in dataset.questions)
        expected = dataset.spec.expected
        expected_questions = expected.question_count
        expected_points = expected.total_points
        if (
            expected_questions is not None
            and (len(dataset.questions) != expected_questions or len(ids) != expected_questions)
        ) or (expected_points is not None and points != expected_points):
            _add(
                checks,
                "dataset_validation",
                "fail",
                "dataset metadata does not match the benchmark specification",
                public_blocking=True,
            )
        else:
            _add(
                checks,
                "dataset_validation",
                "pass",
                "%d-question/%d-point metadata loads without requiring local image assets"
                % (len(dataset.questions), points),
                public_blocking=False,
            )
    else:
        _add(
            checks,
            "dataset_validation",
            "pass",
            "%d questions, %d points, manifest hashes validated"
            % (len(dataset.questions), sum(int(q["points"]) for q in dataset.questions)),
        )

        answer_audit = _read_jsonl(_evidence_path(root, manifest, "answer_audit.jsonl"))
        if len(answer_audit) != len(dataset.questions):
            _add(checks, "answer_audit", "fail", "answer audit covers %d/%d questions" % (len(answer_audit), len(dataset.questions)), public_blocking=True)
        else:
            status_counts: Dict[str, int] = {}
            for row in answer_audit:
                status = str(row.get("status", "unspecified"))
                status_counts[status] = status_counts.get(status, 0) + 1
            pending = sum(count for status, count in status_counts.items() if "pending" in status or status == "ocr_match_pending_visual")
            detail = "%d/%d answer audit rows; statuses=%s; pending_visual=%d" % (
                len(answer_audit),
                len(dataset.questions),
                json.dumps(status_counts, ensure_ascii=False, sort_keys=True),
                pending,
            )
            if pending and public_full:
                _add(
                    checks,
                    "answer_audit",
                    "fail",
                    detail + "; signatures must be written into a new immutable dataset version before public release",
                    public_blocking=True,
                )
            elif pending:
                _add(checks, "answer_audit", "warning", detail + "; internal signoff overlay may proceed", public_blocking=False)
            else:
                _add(checks, "answer_audit", "pass", detail, public_blocking=True)

        point_audit = _read_jsonl(_evidence_path(root, manifest, "point_audit.jsonl"))
        pending_points = sum(
            1 for row in point_audit if "pending" in str(row.get("status", "")).lower()
        )
        if len(point_audit) != len(dataset.questions):
            _add(checks, "point_audit", "fail", "point audit covers %d/%d items" % (len(point_audit), len(dataset.questions)), public_blocking=True)
        elif pending_points and public_full:
            _add(
                checks,
                "point_audit",
                "fail",
                "%d/%d point rows are still pending; write signoff into a new immutable dataset version" % (pending_points, len(point_audit)),
                public_blocking=True,
            )
        elif pending_points:
            _add(checks, "point_audit", "warning", "%d/%d point rows are pending owner signoff" % (pending_points, len(point_audit)), public_blocking=False)
        else:
            _add(checks, "point_audit", "pass", "%d/%d point rows are complete" % (len(point_audit), len(point_audit)), public_blocking=True)

        quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
        owner_attested = quality.get("review_protocol") == "owner_attested_single_review"
        if public_full:
            stale_quality = []
            if owner_attested:
                for field in ("owner_signoff", "review_status"):
                    if quality.get(field) not in {"audited_passed", "complete", "passed"}:
                        stale_quality.append("%s=%s" % (field, quality.get(field)))
                for field in ("answer_status", "point_status", "asset_status", "question_audit_status"):
                    if str(quality.get(field, "")).lower() not in {"audited_passed", "complete", "passed"}:
                        stale_quality.append("%s=%s" % (field, quality.get(field)))
            else:
                for field in ("owner_signoff", "human_independent_review"):
                    if quality.get(field) != "complete":
                        stale_quality.append("%s=%s" % (field, quality.get(field)))
                for field in ("answer_status", "point_status"):
                    if "pending" in str(quality.get(field, "")).lower():
                        stale_quality.append("%s=%s" % (field, quality.get(field)))
            if stale_quality:
                _add(
                    checks,
                    "dataset_quality_closure",
                    "fail",
                    "dataset metadata is still pre-signoff: %s" % ", ".join(stale_quality),
                    public_blocking=True,
                )
            else:
                detail = (
                    "dataset metadata records a completed single-owner audit"
                    if owner_attested
                    else "dataset metadata records completed human review"
                )
                _add(checks, "dataset_quality_closure", "pass", detail, public_blocking=True)

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
    owner_attested = quality.get("review_protocol") == "owner_attested_single_review"
    independent_status = "not_applicable" if owner_attested else "pending"
    if not public_code:
        if owner_attested:
            owner = _owner_attestation_check(
                root,
                manifest,
                expected_items=len(dataset.questions),
                expected_points=sum(int(question["points"]) for question in dataset.questions),
            )
            _add(checks, "owner_signoff", owner["status"], owner["detail"], public_blocking=True)
            _add(
                checks,
                "review_protocol",
                "pass" if owner["status"] == "pass" else "fail",
                "owner_attested_single_review; independent review is explicitly not claimed",
                public_blocking=True,
            )
            _add(
                checks,
                "independent_review",
                "pass",
                "not applicable: the dataset declares a single-owner attestation protocol",
                public_blocking=public_full,
            )
            _add(
                checks,
                "reviewer_separation",
                "pass",
                "not applicable: no independent-reviewer identities are claimed",
                public_blocking=public_full,
            )
        else:
            owner_report = owner_report or default_reports["owner_report"]
            owner = _review_check(
                owner_report,
                manifest_sha=manifest_sha,
                expected_items=len(dataset.questions),
                kind="owner",
                public_blocking=True,
                lineage=review_lineage,
                current_signed_content=current_signed_content,
            )
            _add(checks, "owner_signoff", owner["status"], owner["detail"], public_blocking=True)

            independent_report = independent_report or default_reports["independent_report"]
            independent = _review_check(
                independent_report,
                manifest_sha=manifest_sha,
                expected_items=len(dataset.questions),
                kind="independent",
                public_blocking=True,
                lineage=review_lineage,
                current_signed_content=current_signed_content,
            )
            independent_status = independent["status"]
            # Internal alpha can be demonstrated before the independent review, but
            # the full public target always requires it.
            _add(
                checks,
                "independent_review",
                independent["status"] if public_full else "warning",
                independent["detail"] if public_full else "independently reviewed: %s (internal warning only)" % independent["detail"],
                public_blocking=public_full,
            )
            if owner["status"] == "pass" and independent["status"] == "pass":
                owner_payload = _read_json(owner_report) or {}
                independent_payload = _read_json(independent_report) or {}
                owner_identity = owner_payload.get("reviewer") if isinstance(owner_payload.get("reviewer"), dict) else {}
                reviewers = independent_payload.get("reviewers") if isinstance(independent_payload.get("reviewers"), dict) else {}
                collisions = []
                for slot in ("1", "2"):
                    reviewer = reviewers.get(slot) if isinstance(reviewers.get(slot), dict) else {}
                    if owner_identity.get("reviewer_id") == reviewer.get("reviewer_id"):
                        collisions.append("owner and reviewer-%s share reviewer_id" % slot)
                    if owner_identity.get("reviewer_name") == reviewer.get("reviewer_name"):
                        collisions.append("owner and reviewer-%s share reviewer_name" % slot)
                if collisions:
                    _add(
                        checks,
                        "reviewer_separation",
                        "fail" if public_full else "warning",
                        "; ".join(collisions),
                        public_blocking=public_full,
                    )
                else:
                    _add(
                        checks,
                        "reviewer_separation",
                        "pass",
                        "owner and two independent reviewers are three distinct identities",
                        public_blocking=public_full,
                    )

    if not public_code:
        crop = _prompt_crop_check(root)
        _add(checks, "prompt_crop_integrity", crop["status"], crop["detail"], public_blocking=True)

        smoke_path = Path(smoke_report) if smoke_report else default_reports["smoke_report"]
        smoke = _smoke_check(
            smoke_path,
            manifest_sha=manifest_sha,
            dataset_version=dataset_version,
            expected_items=len(dataset.questions),
            possible_points=sum(int(question["points"]) for question in dataset.questions),
        )
        _add(checks, "smoke_report", smoke["status"], smoke["detail"], public_blocking=True)

    run = _read_json(Path(run_summary)) if run_summary else None
    run_check = _clean_run_check(run, manifest_sha) if run else {"status": "fail", "detail": "no clean model run supplied"}
    if public_full:
        run_detail = run_check["detail"]
        if run_check["status"] != "pass":
            run_detail += (
                "; the immutable dataset may still be published, but no model score or "
                "public leaderboard entry may be released from this evidence"
            )
        _add(
            checks,
            "clean_model_run",
            run_check["status"] if run_check["status"] == "pass" else "warning",
            run_detail,
            public_blocking=False,
        )
    elif not public_code:
        _add(checks, "clean_model_run", "warning", run_check["detail"], public_blocking=False)

    if public_full:
        answer_check = _answer_protocol_check(
            manifest,
            policy=policy if policy_bound else None,
            manifest_sha=manifest_sha,
            dataset_version=dataset_version,
            independent_pass=independent_status == "pass",
            owner_attested=owner_attested,
            protocol_path=answer_protocol,
            source_root=source_root,
        )
        _add(checks, "official_answer_source", answer_check["status"], answer_check["detail"], public_blocking=True)
        if answer_check.get("warning"):
            _add(checks, "official_answer_source_notice", "warning", answer_check["detail"], public_blocking=False)
    elif not public_code:
        official_answer = bool((manifest.get("quality") or {}).get("official_answer_source_found"))
        if official_answer:
            _add(checks, "official_answer_source", "pass", "official answer source is recorded", public_blocking=False)
        elif owner_attested:
            _add(
                checks,
                "official_answer_source",
                "warning",
                "no official answer source; the single-owner answer attestation is closed and independent review is not claimed",
                public_blocking=False,
            )
        else:
            _add(checks, "official_answer_source", "warning", "official answer source not found; independent review is required for full public data", public_blocking=False)

    if public_code:
        rights_check = _public_code_rights_check(policy if policy_bound else None, source_root)
        _add(checks, "public_rights", rights_check["status"], rights_check["detail"], public_blocking=True)
    elif public_full:
        rights_check = _full_public_rights_check(
            manifest,
            policy if policy_bound else None,
            manifest_sha=manifest_sha,
            dataset_version=dataset_version,
            attestation_path=rights_attestation,
            source_root=source_root,
        )
        _add(checks, "public_rights", rights_check["status"], rights_check["detail"], public_blocking=True)
        if rights_check.get("warning"):
            _add(checks, "public_rights_notice", "warning", rights_check["detail"], public_blocking=False)
    else:
        _add(checks, "public_rights", "warning", "internal profile only; public redistribution is not cleared", public_blocking=False)

    if public_any:
        contamination_check = _contamination_check(
            policy if policy_bound else None,
            manifest_sha=manifest_sha,
            dataset_version=dataset_version,
            source_root=source_root,
            dataset_years=dataset.spec.years,
        )
        _add(checks, "contamination", contamination_check["status"], contamination_check["detail"], public_blocking=True)
    else:
        _add(
            checks,
            "contamination",
            "warning",
            "public %s items are potentially contaminated; no fresh-ability claim is allowed"
            % "/".join(str(year) for year in dataset.spec.years),
            public_blocking=False,
        )

    git = _git_state(Path(repo_root).resolve())
    git_detail = "HEAD=%s; dirty=%s" % (git.get("head"), git.get("dirty"))
    if public_any and (not git.get("available") or git.get("dirty")):
        _add(checks, "source_revision", "fail", git_detail + "; public release needs a clean commit/tag", public_blocking=True)
    elif public_any:
        _add(checks, "source_revision", "pass", git_detail, public_blocking=True)
    else:
        _add(checks, "source_revision", "warning", git_detail + "; internal review can proceed, but not a tagged release", public_blocking=False)

    blocking = [
        {"name": row["name"], "status": row["status"], "detail": row["detail"]}
        for row in checks
        if row["public_blocking"] and row["status"] == "fail"
    ]
    warnings = [
        {"name": row["name"], "status": row["status"], "detail": row["detail"]}
        for row in checks
        if row["status"] == "warning"
    ]
    status = "blocked" if blocking else "pass"
    if not blocking:
        next_actions: List[str] = []
    elif public_code:
        next_actions = [
            "Freeze a clean commit/tag for the code-and-documentation-only public release.",
            "Build the public-code bundle and verify that it contains no data, assets, or third-party evidence.",
        ]
    elif owner_attested:
        next_actions = [
            "Resolve the blocking checks above; the single-owner audit is already closed.",
            "Run validate, smoke, pytest, gold scoring, and a fresh vision run with the fixed harness.",
            "Sign the rights decision if the full public dataset is to be released.",
            "Freeze a clean commit/tag and only then consider a public release.",
        ]
    else:
        next_actions = [
            "Owner completes %d/%d review rows and imports a signed owner report."
            % (len(dataset.questions), len(dataset.questions)),
            "Two independent reviewers complete answer and point reviews without conflicts.",
            "Run validate, smoke, pytest, gold scoring, and a fresh vision run with the fixed harness.",
            "Sign the rights decision and answer-validation protocol if the full public dataset is to be released.",
            "Freeze a clean commit/tag and only then consider a public release.",
        ]
    return {
        "schema_version": "1.0",
        "target": target,
        "status": status,
        "dataset_version": dataset_version,
        "harness_version": HARNESS_VERSION,
        "scorer_version": SCORER_VERSION,
        "prompt_version": dataset.spec.prompt.version,
        "dataset_manifest_sha256": manifest_sha,
        "checks": checks,
        "blocking_checks": blocking,
        "warnings": warnings,
        "next_actions": next_actions,
    }


def write_release_report(report: Dict[str, Any], output: Path | str) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path

