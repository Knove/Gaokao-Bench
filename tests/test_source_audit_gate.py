import copy
import hashlib
import json
from pathlib import Path

import pytest

from gaokao_bench.scoring import Dataset, validate_source_audit


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x01\x05\x01\x02"
    b"\x8a\x13\x8c\xb8\x00\x00\x00\x00IEND\xaeB`\x82"
)


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
                "url": "https://%s.example/1" % source_id,
                "scope": scope,
                "page_index": 1,
                "covered_page_starts": [1],
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


def test_rejected_wrong_answer_source_counts_for_stem_but_not_answer(tmp_path):
    dataset = _build_dataset(tmp_path, include_rejected=True)
    summary = validate_source_audit(dataset)
    assert summary["gate"] == "pass"
    row = json.loads((tmp_path / "source_audit.jsonl").read_text(encoding="utf-8"))
    rejected = next(item for item in row["assessments"] if item["source_id"] == "src-rejected-5")
    assert rejected["role"] == "rejected"
    assert rejected["answer_match"] == "external_answer_error"
    assert rejected["answer_value"] == "B"


def test_prefix_stem_excerpt_is_rejected(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["assessments"][0]["stem_excerpt"] = "若 1+1"

    _rewrite_source_audit(dataset, mutate)
    with pytest.raises(ValueError, match="exact canonical stem"):
        validate_source_audit(dataset)


def test_answer_excerpt_cannot_append_free_text(tmp_path):
    dataset = _build_dataset(tmp_path)

    def mutate(rows):
        rows[0]["assessments"][0]["answer_excerpt"] = "A；来源页含完整证明或解答过程。"

    _rewrite_source_audit(dataset, mutate)
    with pytest.raises(ValueError, match="exact canonical answer"):
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
