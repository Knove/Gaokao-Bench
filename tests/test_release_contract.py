import json
from pathlib import Path

import pytest

from gaokao_bench.harness import FixedResponseProvider, ProviderResponse, run_benchmark
from gaokao_bench.public_data import build_public_data_package
from gaokao_bench.release import evaluate_release_gate
from gaokao_bench.scoring import (
    PUBLIC_PACKAGE_PROFILE,
    evaluate_submission,
    file_sha256,
    load_dataset,
    score_question,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "0.0.4"

def _source_release_ready() -> bool:
    if not (DATA / "manifest.json").is_file():
        return False
    try:
        manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        manifest.get("package_profile") != PUBLIC_PACKAGE_PROFILE
        and (DATA / "answer_audit.jsonl").is_file()
        and (DATA / "question_audit.jsonl").is_file()
    )


pytestmark = [
    pytest.mark.source_release,
    pytest.mark.skipif(
        not _source_release_ready(),
        reason=(
            "internal source-release package is not present; public data packages do not "
            "contain source-only lineage and root-level audit files"
        ),
    ),
]


class ReproducibleMockProvider:
    def complete(self, **kwargs):
        return ProviderResponse(
            text='{"answer": "A"}',
            model="mock-model",
            usage={"input_tokens": 10, "output_tokens": 5},
            response_id="mock-response",
            finish_reason="stop",
            attempts=1,
            upstream_provider="Mock",
            system_fingerprint="mock-fingerprint",
            seed=7,
        )


def test_0_0_4_has_one_metric_and_closed_audit_statuses():
    dataset = load_dataset(DATA)
    manifest = dataset.manifest
    spec = json.loads((DATA / "benchmark_spec.json").read_text(encoding="utf-8"))
    assert manifest["scoring"]["primary_metric"] == spec["scoring"]["primary_metric"]
    assert manifest["scoring"]["contract"] == "single_primary_metric_v1"
    assert "main_metric" not in manifest["scoring"]
    assert manifest["status"] == "audited_passed"
    assert manifest["audit"]["status"] == "audited_passed"
    assert manifest["review_evidence"]["status"] == "audited_passed"
    for field in (
        "owner_signoff",
        "review_status",
        "answer_status",
        "point_status",
        "asset_status",
        "question_audit_status",
    ):
        assert manifest["quality"][field] == "audited_passed"
    assert manifest["quality"]["independent_review"] == "not_required_single_owner_attestation"
    assert manifest["versions"] == manifest["compatibility"]
    lineage = manifest["content_lineage"]
    assert lineage["source_dataset_version"] == "0.0.3"
    for group in ("content_files", "audit_files"):
        for relative, digest in lineage[group].items():
            assert file_sha256(DATA / relative) == digest
    assert [row["id"] for row in manifest["leaderboard"]["tracks"]] == [
        "retrospective_public_2025",
        "post_cutoff_live",
    ]


def test_shanghai_q11_rounding_and_borderline_semantics():
    dataset = load_dataset(DATA)
    question = next(q for q in dataset.questions if q["id"] == "GB25-SH-Q11")
    key = dataset.answer_keys["GB25-SH-Q11"]
    accepted = score_question(question, key, "12.58°", True)
    assert accepted["status"] == "correct"
    for value in ("12.57", "12.59"):
        result = score_question(question, key, value, True)
        assert result["status"] == "incorrect"
        assert result["borderline_candidate"] is True


def test_gold_scoring_uses_the_declared_primary_metric():
    dataset = load_dataset(DATA)
    gold = json.loads((DATA / "examples" / "gold.json").read_text(encoding="utf-8"))
    report = evaluate_submission(dataset, gold["answers"])
    assert report["summary"]["earned_points"] == 353
    assert report["summary"]["possible_points"] == 353
    assert report["primary_metric"]["id"] == dataset.spec.scoring.primary_metric
    assert report["primary_metric"]["value"] == 1.0


def test_owner_attestation_is_a_closed_gate_condition(tmp_path):
    report = evaluate_release_gate(
        DATA,
        target="internal",
        smoke_report=tmp_path / "missing-smoke.json",
        repo_root=tmp_path,
    )
    checks = {row["name"]: row for row in report["checks"]}
    assert checks["owner_signoff"]["status"] == "pass"
    assert checks["review_protocol"]["status"] == "pass"
    assert checks["independent_review"]["status"] == "pass"
    assert "not applicable" in checks["independent_review"]["detail"]
    assert "reviewer_separation" in checks and checks["reviewer_separation"]["status"] == "pass"


def test_summary_exposes_fixed_denominator_versions_and_split_tracks(tmp_path):
    dataset = load_dataset(DATA)
    summary = run_benchmark(
        dataset,
        FixedResponseProvider('{"answer": "A"}'),
        provider_name="mock",
        model="mock-model",
        modality="vision",
        repeats=2,
        output_dir=tmp_path / "run",
        paper_ids=["GB25-N1"],
        created_at="2026-11-10T18:00:00+08:00",
    )
    assert summary["aggregate"]["possible_points"] == summary["scope"]["selected_possible_points"]
    assert summary["aggregate"]["questions"] == summary["scope"]["selected_questions"]
    assert summary["compatibility"]["dataset"] == "0.0.4"
    assert summary["versions"] == summary["compatibility"]
    assert summary["leaderboard"]["tracks"][0]["id"] == "retrospective_public_2025"
    assert summary["leaderboard"]["tracks"][1]["id"] == "post_cutoff_live"
    assert summary["reproducibility"]["canonical_eligible"] is False
    assert summary["result_status"] == "pilot"
    assert summary["modality_summary"]["vision_standalone_ranking_allowed"] is False
    assert "bootstrap_95_ci" in summary["slices"]["vision_required"]


def test_canonical_status_requires_unconstrained_reasoning_profile(tmp_path):
    dataset = load_dataset(DATA)
    canonical = run_benchmark(
        dataset,
        ReproducibleMockProvider(),
        provider_name="mock",
        model="mock-model",
        modality="text",
        repeats=5,
        output_dir=tmp_path / "canonical",
        paper_ids=["GB25-N1"],
        limit=2,
        timeout=900,
        max_tokens=32768,
        seed=7,
        provider_order=["Mock"],
        allow_fallbacks=False,
        model_snapshot="mock-2026-11-10",
        created_at="2026-11-10T18:00:00+08:00",
    )
    assert canonical["reproducibility"]["canonical_eligible"] is True
    assert canonical["result_status"] == "canonical"

    constrained = run_benchmark(
        dataset,
        ReproducibleMockProvider(),
        provider_name="mock",
        model="mock-model",
        modality="text",
        repeats=5,
        output_dir=tmp_path / "constrained",
        paper_ids=["GB25-N1"],
        limit=2,
        timeout=120,
        max_tokens=8192,
        reasoning_budget=4096,
        seed=7,
        provider_order=["Mock"],
        allow_fallbacks=False,
        model_snapshot="mock-2026-11-10",
        created_at="2026-11-10T18:00:00+08:00",
    )
    assert constrained["reproducibility"]["canonical_eligible"] is False
    assert constrained["result_status"] == "pilot"
    caveats = " ".join(constrained["reproducibility"]["caveats"])
    assert "timeout is below" in caveats
    assert "max_tokens is below" in caveats
    assert "constrained reasoning track" in caveats

    nonzero_temperature = run_benchmark(
        dataset,
        ReproducibleMockProvider(),
        provider_name="mock",
        model="mock-model",
        modality="text",
        repeats=5,
        output_dir=tmp_path / "nonzero-temperature",
        paper_ids=["GB25-N1"],
        limit=2,
        timeout=900,
        max_tokens=32768,
        temperature=0.7,
        seed=7,
        provider_order=["Mock"],
        allow_fallbacks=False,
        model_snapshot="mock-2026-11-10",
        created_at="2026-11-10T18:00:00+08:00",
    )
    assert nonzero_temperature["reproducibility"]["canonical_eligible"] is False
    assert nonzero_temperature["result_status"] == "pilot"
    assert "temperature must be exactly 0" in " ".join(
        nonzero_temperature["reproducibility"]["caveats"]
    )


def test_public_package_audit_layout_is_self_verifying(tmp_path):
    output = tmp_path / "0.0.4"
    build_public_data_package(
        DATA,
        output,
        repo_id="Knove/Gaokao-Bench",
        created_at="2026-11-10T10:00:00+00:00",
    )
    public_manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert public_manifest["status"] == "audited_passed"
    assert public_manifest["package_profile"] == "public-data"
    assert public_manifest["audit"]["status"] == "audited_passed"
    assert public_manifest["review_evidence"]["status"] == "audited_passed"
    report = evaluate_release_gate(
        output,
        target="public",
        smoke_report=tmp_path / "missing-smoke.json",
        repo_root=tmp_path,
    )
    checks = {row["name"]: row for row in report["checks"]}
    for name in (
        "answer_audit",
        "point_audit",
        "owner_signoff",
        "review_protocol",
        "dataset_quality_closure",
        "official_answer_source",
    ):
        assert checks[name]["status"] == "pass", (name, checks[name])
    signoff = json.loads((output / "audit" / "owner_signoff.json").read_text(encoding="utf-8"))
    assert signoff["review_type"] == "derived_public_projection_owner_attestation"
    assert signoff["status"] == "audited_passed"
    assert signoff["input_sha256"]["audit/reviews.jsonl"] == file_sha256(
        output / "audit" / "reviews.jsonl"
    )
