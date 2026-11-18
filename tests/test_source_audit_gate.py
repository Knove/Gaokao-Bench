import copy
import hashlib
import json
from pathlib import Path

import pytest

import gaokao_bench.scoring as scoring
from gaokao_bench.quality import quality_level_errors, source_audit_policy
from gaokao_bench.scoring import Dataset, validate_source_audit


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x01\x05\x01\x02"
    b"\x8a\x13\x8c\xb8\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_quality_level_requires_strict_policy_floor():
    manifest = {
        "quality": {"quality_level": "strict_dual_source_v4"},
        "source_audit": {"policy": source_audit_policy()},
    }
    assert quality_level_errors(manifest, require_source_audit_quality=True) == []

    legacy = copy.deepcopy(manifest)
    legacy["source_audit"]["policy"]["version"] = "dual_source_v2"
    legacy["source_audit"]["policy"]["required_stem_independent_groups"] = 2
    legacy["source_audit"]["policy"]["required_joint_answer_independent_groups"] = 2
    errors = quality_level_errors(legacy, require_source_audit_quality=True)
    assert any("policy version" in error for error in errors)
    assert any("nine independent stem groups" in error for error in errors)
    assert any("six independent joint groups" in error for error in errors)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assessment(
    source_id: str,
    group: str,
    *,
    scope: str = "stem_and_answer",
    evidence_sha256: str,
    evidence_bytes: int,
    rejected: bool = False,
):
    evidence = [
        {
            "path": "source_pages/%s/page-01.png" % source_id,
            "url": "https://%s.example/1" % source_id,
            "sha256": evidence_sha256,
            "bytes": evidence_bytes,
        }
    ]
    return {
        "source_id": source_id,
        "publisher": source_id,
        "url": "https://%s.example" % source_id,
        "independence_group": group,
        "evidence_scope": scope,
        "role": "rejected" if rejected else "joint_supporting",
        "supports_stem": True,
        "supports_canonical": False if rejected else True,
        "stem_match": "exact_stem_match",
        "answer_match": "external_answer_error" if rejected else "exact_answer_match",
        "stem_locator": "第1页；第1题",
        "answer_locator": "第1页；第1题",
        "stem_excerpt": "若 1+1=？",
        "answer_value": "B" if rejected else "A",
        "answer_excerpt": "B" if rejected else "A",
        "source_answer_text": "B" if rejected else "A",
        "rejection_reason": "来源答案写成 B，正确结论为 A。" if rejected else None,
        "access_method": "逐页核对",
        "stem_evidence": copy.deepcopy(evidence),
        "answer_evidence": copy.deepcopy(evidence),
        "stem_page_url": "https://%s.example/1" % source_id,
        "answer_page_url": "https://%s.example/1" % source_id,
        "stem_page_start": 1,
        "stem_page_end": 1,
        "answer_page_start": 1,
        "answer_page_end": 1,
    }


def _build_dataset(tmp_path: Path, *, include_rejected: bool = False) -> Dataset:
    source_ids = ["src-joint-1", "src-joint-2", "src-stem-3", "src-stem-4"]
    if include_rejected:
        source_ids.append("src-rejected-5")
    evidence_rows = []
    manifest_files = {}
    for index, source_id in enumerate(source_ids, 1):
        relative = "source_pages/%s/page-01.png" % source_id
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG_BYTES + bytes([index]))
        payload = path.read_bytes()
        scope = "stem_and_answer" if index <= 2 or source_id == "src-rejected-5" else "stem"
        evidence_rows.append(
            {
                "source_id": source_id,
                "path": relative,
                "file": relative.removeprefix("source_pages/"),
                "url": "https://%s.example/1" % source_id,
                "scope": scope,
                "page_index": 1,
                "covered_page_starts": [1],
                "media_type": "image/png",
                "question_numbers": [1],
                "sha256": _sha256(payload),
                "bytes": len(payload),
            }
        )
        manifest_files[relative] = {"sha256": _sha256(payload), "bytes": len(payload)}

    _write_jsonl(tmp_path / "source_pages.jsonl", evidence_rows)
    evidence_digest = _sha256((tmp_path / "source_pages.jsonl").read_bytes())
    manifest_files["source_pages.jsonl"] = {
        "sha256": evidence_digest,
        "bytes": (tmp_path / "source_pages.jsonl").stat().st_size,
    }
    sources = []
    for index, source_id in enumerate(source_ids, 1):
        sources.append(
            {
                "id": source_id,
                "publisher": source_id,
                "url": "https://%s.example" % source_id,
                "evidence_scope": (
                    "stem_and_answer" if index <= 2 or source_id == "src-rejected-5" else "stem"
                ),
                "independence_group": "group-%d" % index,
                "access_method": "逐页核对",
            }
        )
    _write_jsonl(tmp_path / "sources.jsonl", sources)

    evidence_by_source = {row["source_id"]: row for row in evidence_rows}
    assessments = [
        _assessment(
            "src-joint-1",
            "group-1",
            evidence_sha256=evidence_by_source["src-joint-1"]["sha256"],
            evidence_bytes=evidence_by_source["src-joint-1"]["bytes"],
        ),
        _assessment(
            "src-joint-2",
            "group-2",
            evidence_sha256=evidence_by_source["src-joint-2"]["sha256"],
            evidence_bytes=evidence_by_source["src-joint-2"]["bytes"],
        ),
        _assessment(
            "src-stem-3",
            "group-3",
            scope="stem",
            evidence_sha256=evidence_by_source["src-stem-3"]["sha256"],
            evidence_bytes=evidence_by_source["src-stem-3"]["bytes"],
        ),
        _assessment(
            "src-stem-4",
            "group-4",
            scope="stem",
            evidence_sha256=evidence_by_source["src-stem-4"]["sha256"],
            evidence_bytes=evidence_by_source["src-stem-4"]["bytes"],
        ),
    ]
    for item in assessments[2:]:
        item["role"] = "stem_supporting"
        item["supports_canonical"] = False
        item["answer_match"] = "not_assessed"
        item["answer_value"] = ""
        item["answer_excerpt"] = ""
        item["source_answer_text"] = ""
        item["answer_evidence"] = []
        item["answer_page_url"] = ""
        item["answer_page_start"] = None
        item["answer_page_end"] = None
        item["answer_locator"] = ""
    if include_rejected:
        assessments.append(
            _assessment(
                "src-rejected-5",
                "group-5",
                evidence_sha256=evidence_by_source["src-rejected-5"]["sha256"],
                evidence_bytes=evidence_by_source["src-rejected-5"]["bytes"],
                rejected=True,
            )
        )

    stem_groups = 5 if include_rejected else 4
    _write_jsonl(
        tmp_path / "source_audit.jsonl",
        [
            {
                "question_id": "Q1",
                "paper_id": "P1",
                "points": 5,
                "canonical_answer": "A",
                "stem_independent_groups": stem_groups,
                "joint_answer_independent_groups": 2,
                "stem_dual_source_complete": True,
                "joint_answer_dual_source_complete": True,
                "gate_complete": True,
                "rejected_source_ids": ["src-rejected-5"] if include_rejected else [],
                "assessments": assessments,
            }
        ],
    )
    manifest = {
        "files": manifest_files,
        "source_audit": {
            "evidence_pages": "source_pages.jsonl",
            "evidence_pages_sha256": evidence_digest,
            "evidence_page_count": len(evidence_rows),
            "policy": {
                "version": "dual_source_v2",
                "required_stem_independent_groups": 2,
                "required_joint_answer_independent_groups": 2,
                "answer_only_sources_count_for_gate": False,
                "required_source_page_evidence": True,
            },
            "questions": 1,
            "stem_dual_source_questions": 1,
            "joint_answer_dual_source_questions": 1,
            "gate_complete_questions": 1,
            "evidence_ready": True,
            "gate": "pass",
        },
    }
    return Dataset(
        data_dir=tmp_path,
        manifest=manifest,
        papers=(),
        questions=(
            {
                "id": "Q1",
                "paper_id": "P1",
                "number": 1,
                "points": 5,
                "stem": "若 1+1=？",
                "type": "single_choice",
            },
        ),
        answer_keys={"Q1": {"canonical": "A"}},
        catalog_subjective=(),
        spec=None,
    )


def _rewrite_source_audit(dataset: Dataset, mutate) -> None:
    path = dataset.data_dir / "source_audit.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    mutate(rows)
    _write_jsonl(path, rows)


def _rewrite_evidence(dataset: Dataset, mutate) -> None:
    path = dataset.data_dir / "source_pages.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    mutate(rows)
    _write_jsonl(path, rows)
    digest = _sha256(path.read_bytes())
    dataset.manifest["files"]["source_pages.jsonl"] = {
        "sha256": digest,
        "bytes": path.stat().st_size,
    }
    dataset.manifest["source_audit"]["evidence_pages_sha256"] = digest
    dataset.manifest["source_audit"]["evidence_page_count"] = len(rows)


def _manifest_file_metadata(path: Path) -> dict:
    payload = path.read_bytes()
    return {"sha256": _sha256(payload), "bytes": len(payload)}


def _enable_pinned_specs(
    dataset: Dataset, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    monkeypatch.setattr(scoring, "REPOSITORY_ROOT", repository)

    version = "0.0.1-test"
    dataset.manifest["dataset_version"] = version
    policy = copy.deepcopy(dataset.manifest["source_audit"]["policy"])
    evidence_rows = [
        json.loads(line)
        for line in (tmp_path / "source_pages.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    pages_spec = {"schema_version": "1.0", "dataset_version": version, "sources": {}}
    audit_spec = {
        "schema_version": "1.0",
        "dataset_version": version,
        "policy": policy,
        "sources": [
            {"id": "src-joint-1", "independence_group": "group-1"},
            {"id": "src-joint-2", "independence_group": "group-2"},
            {"id": "src-stem-3", "independence_group": "group-3"},
            {"id": "src-stem-4", "independence_group": "group-4"},
        ],
        "coverage": {
            "src-joint-1": {
                "observed_answers": {"1": {"value": "A", "excerpt": "A"}}
            },
            "src-joint-2": {
                "observed_answers": {"1": {"value": "A", "excerpt": "A"}}
            },
            "src-stem-3": {"stem_groups": []},
            "src-stem-4": {"stem_groups": []},
        },
    }
    for row in evidence_rows:
        pages_spec["sources"].setdefault(row["source_id"], []).append(
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

    package_pages = tmp_path / "audit" / "specs" / "source-pages.json"
    package_audit = tmp_path / "audit" / "specs" / "source-audit.json"
    repo_pages = repository / "specs" / "source-pages.json"
    repo_audit = repository / "specs" / "source-audit.json"
    _write_json(package_pages, pages_spec)
    _write_json(package_audit, audit_spec)
    _write_json(repo_pages, pages_spec)
    _write_json(repo_audit, audit_spec)

    block = dataset.manifest["source_audit"]
    block.update(
        {
            "require_pinned_specs": True,
            "pinned_pages_spec": "audit/specs/source-pages.json",
            "pinned_pages_spec_sha256": _manifest_file_metadata(package_pages)["sha256"],
            "pinned_audit_spec": "audit/specs/source-audit.json",
            "pinned_audit_spec_sha256": _manifest_file_metadata(package_audit)["sha256"],
        }
    )
    dataset.manifest["files"]["audit/specs/source-pages.json"] = _manifest_file_metadata(
        package_pages
    )
    dataset.manifest["files"]["audit/specs/source-audit.json"] = _manifest_file_metadata(
        package_audit
    )
    dataset.manifest["build_provenance"] = {
        "source_pages_spec_path": "specs/source-pages.json",
        "source_pages_spec_sha256": _manifest_file_metadata(repo_pages)["sha256"],
        "source_audit_spec_path": "specs/source-audit.json",
        "source_audit_spec_sha256": _manifest_file_metadata(repo_audit)["sha256"],
    }


def test_rejected_wrong_answer_source_counts_for_stem_but_not_answer(tmp_path):
    dataset = _build_dataset(tmp_path, include_rejected=True)
    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"
    row = json.loads((tmp_path / "source_audit.jsonl").read_text(encoding="utf-8"))
    rejected = next(item for item in row["assessments"] if item["source_id"] == "src-rejected-5")
    assert rejected["role"] == "rejected"
    assert rejected["answer_match"] == "external_answer_error"
    assert rejected["answer_value"] == "B"


def test_owner_attested_metadata_patch_accepts_unchanged_source_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _build_dataset(tmp_path)
    _enable_pinned_specs(dataset, tmp_path, monkeypatch)
    source_version = dataset.manifest["dataset_version"]
    dataset.manifest["dataset_version"] = source_version + ".1"
    dataset.manifest["quality"] = {"review_protocol": "owner_attested_single_review"}
    dataset.manifest["content_lineage"] = {
        "source_dataset_version": source_version,
        "content_changed": False,
    }

    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"


def test_pinned_source_specs_still_reject_unrelated_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = _build_dataset(tmp_path)
    _enable_pinned_specs(dataset, tmp_path, monkeypatch)
    dataset.manifest["dataset_version"] = "0.0.1-unrelated"

    with pytest.raises(ValueError, match="pinned source pages spec version is stale"):
        validate_source_audit(dataset)


def test_rejected_source_may_preserve_contextual_excerpt(tmp_path):
    dataset = _build_dataset(tmp_path, include_rejected=True)

    def mutate(rows):
        rejected = next(
            item
            for item in rows[0]["assessments"]
            if item["source_id"] == "src-rejected-5"
        )
        rejected["answer_excerpt"] = "（B）；故选 B"
        rejected["source_answer_text"] = "（B）；故选 B"

    _rewrite_source_audit(dataset, mutate)
    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"


def test_rejected_source_can_explicitly_not_support_a_mismatched_stem(tmp_path):
    dataset = _build_dataset(tmp_path, include_rejected=True)

    def mutate(rows):
        rejected = next(
            item
            for item in rows[0]["assessments"]
            if item["source_id"] == "src-rejected-5"
        )
        rejected["supports_stem"] = False
        rejected["stem_match"] = "mismatched_stem"
        rejected["stem_excerpt"] = "若 1+1=？"
        rows[0]["stem_independent_groups"] = 4

    _rewrite_source_audit(dataset, mutate)
    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"


def test_prefix_stem_excerpt_is_rejected(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["assessments"][0]["stem_excerpt"] = "若 1+1"

    _rewrite_source_audit(dataset, mutate)
    with pytest.raises(ValueError, match="exact canonical stem"):
        validate_source_audit(dataset)


def test_answer_excerpt_cannot_be_canonical_backfill_placeholder(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        assessment = rows[0]["assessments"][0]
        assessment["answer_excerpt"] = "来源解答结论：A"
        assessment["source_answer_text"] = "来源解答结论：A"

    _rewrite_source_audit(dataset, mutate)
    with pytest.raises(ValueError, match="placeholder rather than a source transcription"):
        validate_source_audit(dataset)


def test_answer_excerpt_may_quote_source_instead_of_echoing_canonical(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        assessment = rows[0]["assessments"][0]
        assessment["answer_excerpt"] = "【答案】A"
        assessment["source_answer_text"] = "【答案】A"

    _rewrite_source_audit(dataset, mutate)
    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"


def test_source_answer_text_must_match_visible_excerpt(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["assessments"][0]["source_answer_text"] = "另一段来源文字"

    _rewrite_source_audit(dataset, mutate)
    with pytest.raises(ValueError, match="answer excerpt and source_answer_text differ"):
        validate_source_audit(dataset)


def test_non_document_evidence_cannot_claim_multiple_pages(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["covered_page_starts"] = [1, 2]

    _rewrite_evidence(dataset, mutate)
    with pytest.raises(ValueError, match="exactly its own logical page"):
        validate_source_audit(dataset)


def test_evidence_must_name_the_question_number(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["question_numbers"] = [2]

    _rewrite_evidence(dataset, mutate)
    with pytest.raises(ValueError, match="does not name question 1"):
        validate_source_audit(dataset)


def test_pinned_page_relabel_is_rejected(tmp_path, monkeypatch):
    dataset = _build_dataset(tmp_path)
    _enable_pinned_specs(dataset, tmp_path, monkeypatch)

    def mutate(rows):
        rows[0]["page_index"] = 2
        rows[0]["covered_page_starts"] = [2]

    _rewrite_evidence(dataset, mutate)
    with pytest.raises(ValueError, match="differs at (page_index|covered_page_starts)"):
        validate_source_audit(dataset)


def test_missing_pinned_spec_is_rejected(tmp_path, monkeypatch):
    dataset = _build_dataset(tmp_path)
    _enable_pinned_specs(dataset, tmp_path, monkeypatch)
    (tmp_path / "audit" / "specs" / "source-pages.json").unlink()

    with pytest.raises(ValueError, match="pinned source pages spec is missing"):
        validate_source_audit(dataset)


def test_source_observation_cannot_be_replaced_in_pinned_spec(tmp_path, monkeypatch):
    dataset = _build_dataset(tmp_path)
    _enable_pinned_specs(dataset, tmp_path, monkeypatch)
    audit_path = tmp_path / "audit" / "specs" / "source-audit.json"
    audit_spec = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_spec["coverage"]["src-joint-1"]["observed_answers"]["1"] = {
        "value": "B",
        "excerpt": "B",
    }
    _write_json(audit_path, audit_spec)
    dataset.manifest["files"]["audit/specs/source-audit.json"] = _manifest_file_metadata(
        audit_path
    )
    dataset.manifest["source_audit"]["pinned_audit_spec_sha256"] = (
        _manifest_file_metadata(audit_path)["sha256"]
    )

    with pytest.raises(ValueError, match="source observation value is not pinned"):
        validate_source_audit(dataset)
