import base64
import csv
import hashlib
import json
from pathlib import Path

from gaokao_bench import cli
from gaokao_bench.acceptance_dashboard import build_acceptance_page, build_acceptance_payload
from gaokao_bench.owner_review import (
    OWNER_REVIEW_FIELDS,
    OWNER_REVIEW_VERSION,
    REVIEW_WORKSHEET_NAME,
)


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _make_dataset(
    data_root,
    version,
    *,
    created_at,
    status="acceptance_candidate",
    owner_signoff="pending",
):
    data_dir = data_root / version
    item_id = version.upper().replace(".", "-") + "-Q01"
    image_path = data_dir / "assets" / "q01.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(PNG_BYTES)
    _write_json(
        data_dir / "manifest.json",
        {
            "dataset": "Gaokao-Bench",
            "dataset_version": version,
            "created_at": created_at,
            "status": status,
            "rights": {"data_license": "CC-BY-4.0"},
            "quality": {
                "answer_status": "source_and_derivation_confirmed",
                "asset_status": "audited_passed",
                "owner_signoff": owner_signoff,
                "point_status": "audited_passed",
            },
        },
    )
    _write_jsonl(
        data_dir / "papers.jsonl",
        [{"id": "paper-test", "title": "测试数学卷", "region": "全国卷 I", "subject": "数学"}],
    )
    question = {
        "id": item_id,
        "paper_id": "paper-test",
        "display_number": "第1题",
        "number": 1,
        "type": "single_choice",
        "points": 5,
        "stem": "若 1+1=？",
        "options": [
            {"label": "A", "text": "2"},
            {"label": "B", "text": "3"},
        ],
        "answer_slot_count": 1,
        "asset_dependency": "none",
        "image_path": "assets/q01.png",
        "image_sha256": "asset-sha",
        "source": {
            "page_number": 1,
            "bbox": [1, 2, 3, 4],
            "crop_method": "synthetic_test",
            "evidence_status": "verified",
            "source_pdf_sha256": "pdf-sha",
        },
    }
    _write_jsonl(data_dir / "questions.jsonl", [question])
    _write_jsonl(
        data_dir / "answer_keys.jsonl",
        [
            {
                "question_id": item_id,
                "canonical": "A",
                "points": 5,
                "kind": "single_choice",
                "answer_status": "source_and_derivation_confirmed",
            }
        ],
    )
    _write_jsonl(
        data_dir / "answer_sources.jsonl",
        [
            {
                "id": "src-primary",
                "publisher": "主来源",
                "url": "https://primary.example/root",
                "pages": [
                    {
                        "page_index": 0,
                        "page_url": "https://primary.example/page",
                        "image_url": "https://primary.example/page.png",
                    }
                ],
            },
            {
                "id": "src-secondary",
                "publisher": "第二来源",
                "url": "https://secondary.example/root",
                "pages": [
                    {
                        "page_index": 0,
                        "page_url": "https://secondary.example/page",
                        "image_url": "https://secondary.example/page.png",
                    }
                ],
            },
        ],
    )
    _write_jsonl(
        data_dir / "answer_audit.jsonl",
        [
            {
                "question_id": item_id,
                "status": "source_and_derivation_confirmed",
                "external_source_id": "src-primary",
                "external_locator": "page-00；第1题",
                "external_excerpt": "主来源答案为 A",
                "secondary_source_id": "src-secondary",
                "secondary_source_locator": "page-00；第1题",
                "secondary_external_canonical": "A",
            }
        ],
    )
    _write_jsonl(
        data_dir / "question_audit.jsonl",
        [
            {
                "question_id": item_id,
                "derivation": "1+1=2，所以选 A。",
                "answer_verdict": "confirmed",
                "stem_verdict": "pdf_exact",
                "option_verdict": "pdf_exact",
                "asset_verdict": "text_sufficient",
                "reviewer": "test-reviewer",
                "reviewed_at": "2026-01-01T00:00:00+08:00",
            }
        ],
    )
    _write_jsonl(
        data_dir / "point_audit.jsonl",
        [{"item_id": item_id, "status": "audited_passed", "locator": "paper_structure"}],
    )
    _write_jsonl(
        data_dir / "asset_audit.jsonl",
        [
            {
                "question_id": item_id,
                "status": "audited_passed",
                "decision": "retain",
                "after_sha256": "asset-sha",
                "before_sha256": "asset-sha",
            }
        ],
    )
    return data_dir, item_id


def _make_packet(review_root, data_dir, item_id, *, signed):
    packet = review_root / ("%s-owner" % data_dir.name)
    packet.mkdir(parents=True)
    manifest_sha = hashlib.sha256((data_dir / "manifest.json").read_bytes()).hexdigest()
    packet_id = "GB-OWNER-%s-%s" % (data_dir.name, manifest_sha[:12])
    row = {field: "" for field in OWNER_REVIEW_FIELDS}
    row.update(
        {
            "item_id": item_id,
            "paper_id": "paper-test",
            "display_number": "第1题",
            "question_type": "single_choice",
            "points": "5",
            "included_in_scoring": "true",
            "review_scope": "deterministic_objective",
            "stem": "若 1+1=？",
            "options": json.dumps([{"label": "A", "text": "2"}], ensure_ascii=False),
            "canonical_answer": json.dumps("A"),
            "answer_evidence": "source_and_derivation_confirmed",
            "answer_source_id": "src-primary",
            "answer_source_url": "https://primary.example/root",
            "answer_page_url": "https://primary.example/page",
            "answer_image_url": "https://primary.example/page.png",
            "answer_locator": "page-00；第1题",
            "answer_excerpt": "主来源答案为 A",
            "secondary_source_id": "src-secondary",
            "secondary_source_url": "https://secondary.example/root",
            "secondary_page_url": "https://secondary.example/page",
            "secondary_image_url": "https://secondary.example/page.png",
            "secondary_locator": "page-00；第1题",
            "secondary_answer_excerpt": "第二来源答案为 A",
            "point_evidence": "audited_passed",
            "point_locator": "paper_structure",
            "image_dependency": "none",
            "decision": "agree" if signed else "",
        }
    )
    with (packet / "owner_review.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OWNER_REVIEW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)
    worksheet_sha = hashlib.sha256((packet / REVIEW_WORKSHEET_NAME).read_bytes()).hexdigest()
    _write_json(
        packet / "owner_signoff.json",
        {
            "schema_version": OWNER_REVIEW_VERSION,
            "dataset_version": data_dir.name,
            "dataset_manifest_sha256": manifest_sha,
            "packet_id": packet_id,
            "review_worksheet": REVIEW_WORKSHEET_NAME,
            "review_worksheet_sha256": worksheet_sha if signed else "",
            "worksheet_items": 1,
            "reviewer_id": "reviewer.test" if signed else "",
            "owner_attested": signed,
            "reviewer_name": "测试负责人" if signed else "",
            "signature": "测试负责人" if signed else "",
            "reviewed_at": "2026-01-02T00:00:00+08:00" if signed else "",
        },
    )
    packet_files = {
        name: {
            "sha256": hashlib.sha256((packet / name).read_bytes()).hexdigest(),
            "bytes": (packet / name).stat().st_size,
        }
        for name in (REVIEW_WORKSHEET_NAME, "owner_signoff.json")
    }
    _write_json(
        packet / "packet_manifest.json",
        {
            "packet_id": packet_id,
            "packet_version": OWNER_REVIEW_VERSION,
            "dataset_version": data_dir.name,
            "dataset_manifest_sha256": manifest_sha,
            "files": packet_files,
        },
    )
    return packet


def _add_dual_source_audit(data_dir, item_id):
    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources = [
        json.loads(line)
        for line in (data_dir / "answer_sources.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for index, source in enumerate(sources):
        source["evidence_scope"] = "stem_and_answer"
        source["independence_group"] = "group-%d" % (index + 1)
    _write_jsonl(data_dir / "answer_sources.jsonl", sources)
    assessments = [
        {
            "source_id": source["id"],
            "publisher": source["publisher"],
            "url": source["url"],
            "independence_group": source["independence_group"],
            "evidence_scope": "stem_and_answer",
            "role": "joint_supporting",
            "supports_stem": True,
            "supports_canonical": True,
            "stem_match": "exact_stem_match",
            "answer_match": "exact_answer_match",
            "stem_locator": "第1题",
            "answer_locator": "第1题",
            "stem_excerpt": "若 1+1=？",
            "answer_value": "A",
            "answer_excerpt": "答案为 A",
            "access_method": "打开来源逐题核对",
        }
        for source in sources
    ]
    _write_jsonl(
        data_dir / "source_audit.jsonl",
        [
            {
                "question_id": item_id,
                "paper_id": "paper-test",
                "points": 5,
                "canonical_answer": "A",
                "assessments": assessments,
                "stem_independent_groups": 2,
                "joint_answer_independent_groups": 2,
                "stem_dual_source_complete": True,
                "joint_answer_dual_source_complete": True,
                "gate_complete": True,
            }
        ],
    )
    manifest["source_audit"] = {
        "path": "source_audit.jsonl",
        "schema_version": "1.0",
        "policy": {
            "version": "dual_source_v1",
            "required_stem_independent_groups": 2,
            "required_joint_answer_independent_groups": 2,
            "answer_only_sources_count_for_gate": False,
        },
        "questions": 1,
        "stem_dual_source_questions": 1,
        "joint_answer_dual_source_questions": 1,
        "gate_complete_questions": 1,
        "evidence_ready": True,
        "gate": "pass",
        "failed_question_ids": [],
    }
    _write_json(manifest_path, manifest)
    return manifest_path


def _fixture(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    archive_dir, archive_item = _make_dataset(
        data_root,
        "v-archive",
        created_at="2026-01-01T00:00:00+08:00",
        status="archived",
        owner_signoff="audited_passed",
    )
    signed_dir, signed_item = _make_dataset(
        data_root,
        "v-signed",
        created_at="2026-02-01T00:00:00+08:00",
        status="acceptance_candidate",
    )
    candidate_dir, candidate_item = _make_dataset(
        data_root,
        "v-candidate",
        created_at="2026-03-01T00:00:00+08:00",
        status="acceptance_candidate",
    )
    _make_packet(review_root, signed_dir, signed_item, signed=True)
    _make_packet(review_root, candidate_dir, candidate_item, signed=False)
    return data_root, review_root, archive_dir, archive_item, signed_item, candidate_item


def test_one_entry_discovers_versions_and_maps_packet_sources(tmp_path):
    data_root, review_root, _, archive_item, signed_item, candidate_item = _fixture(tmp_path)
    notes = tmp_path / "notes.json"
    _write_json(notes, {"by_question": {candidate_item: {"label": "重点", "reason": "测试来源映射"}}})

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=notes,
    )

    assert payload["schema_version"] == "2.0"
    assert payload["default_version"] == "v-candidate"
    assert [item["id"] for item in payload["versions"]] == ["v-candidate", "v-signed", "v-archive"]
    modes = {item["id"]: item["mode"] for item in payload["versions"]}
    assert modes == {"v-candidate": "candidate", "v-signed": "signed", "v-archive": "archive"}
    assert len(payload["assets"]) == 1
    candidate_meta = payload["datasets"]["v-candidate"]["meta"]
    assert candidate_meta["by_paper"]["paper-test"] == {
        "title": "测试数学卷",
        "region": "全国卷 I",
        "questions": 1,
        "points": 5,
    }

    candidate = payload["datasets"]["v-candidate"]["rows"][0]
    assert candidate["item_id"] == candidate_item
    assert candidate["answer_source"] == {
        "id": "src-primary",
        "publisher": "主来源",
        "url": "https://primary.example/root",
        "page_url": "https://primary.example/page",
        "image_url": "https://primary.example/page.png",
        "locator": "page-00；第1题",
        "excerpt": "主来源答案为 A",
    }
    assert candidate["secondary_source"]["id"] == "src-secondary"
    assert candidate["priority"] == {"label": "重点", "reason": "测试来源映射"}
    assert candidate["image_asset"] in payload["assets"]

    archive = payload["datasets"]["v-archive"]["rows"][0]
    assert archive["item_id"] == archive_item
    assert archive["answer_source"]["publisher"] == "主来源"
    assert archive["answer_source"]["page_url"] == "https://primary.example/page"
    assert payload["versions"][2]["license"] == "CC-BY-4.0"
    assert payload["versions"][2]["audits"][-1]["label"] == "已完成"
    assert payload["datasets"]["v-signed"]["meta"]["signed_reviewer"] == "测试负责人"


def test_build_page_is_a_single_version_independent_entry(tmp_path):
    data_root, review_root, *_ = _fixture(tmp_path)
    output = tmp_path / "reports" / "acceptance" / "index.html"

    result = build_acceptance_page(
        data_root=data_root,
        review_root=review_root,
        output=output,
        notes_path=None,
    )

    assert result["versions"] == 3
    assert result["default_version"] == "v-candidate"
    assert result["questions"] == 3
    assert result["assets"] == 1
    page = output.read_text(encoding="utf-8")
    assert "Gaokao-Bench 验收台" in page
    assert 'view === "versions"' in page
    assert "{{STYLES}}" not in page
    assert "{{SCRIPT}}" not in page
    assert "{{DATA_JSON}}" not in page
    assert not (output.parent / "v-candidate" / "index.html").exists()


def test_cli_writes_the_same_canonical_entry(tmp_path, capsys):
    data_root, review_root, *_ = _fixture(tmp_path)
    output = tmp_path / "reports" / "acceptance" / "index.html"

    exit_code = cli.main(
        [
            "acceptance",
            "--data-root",
            str(data_root),
            "--review-root",
            str(review_root),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert Path(result["output"]) == output.resolve()
    assert result["versions"] == 3
    assert output.is_file()


def test_requested_version_missing_fails_instead_of_creating_a_second_page(tmp_path):
    data_root, review_root, *_ = _fixture(tmp_path)
    try:
        build_acceptance_payload(
            data_root=data_root,
            review_root=review_root,
            versions=["does-not-exist"],
        )
    except ValueError as exc:
        assert "requested data versions not found" in str(exc)
    else:
        raise AssertionError("missing version should fail")


def test_missing_answer_key_is_visible_instead_of_silently_omitted(tmp_path):
    data_root = tmp_path / "data"
    _, item_id = _make_dataset(
        data_root,
        "v-missing-key",
        created_at="2026-01-01T00:00:00+08:00",
        status="archived",
    )
    (data_root / "v-missing-key" / "answer_keys.jsonl").write_text("", encoding="utf-8")

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=tmp_path / "review_packets",
        notes_path=None,
    )

    rows = payload["datasets"]["v-missing-key"]["rows"]
    assert len(rows) == 1
    assert rows[0]["item_id"] == item_id
    assert rows[0]["answer_status"] == "missing_answer_key"
    assert rows[0]["answer_status_label"] == "缺少答案键"


def test_source_gate_is_recomputed_instead_of_trusting_stale_manifest(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    data_dir, item_id = _make_dataset(
        data_root,
        "v-source-audit",
        created_at="2026-04-01T00:00:00+08:00",
        status="acceptance_candidate",
    )
    _add_dual_source_audit(data_dir, item_id)
    _make_packet(review_root, data_dir, item_id, signed=False)

    audit_path = data_dir / "source_audit.jsonl"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["assessments"] = audit["assessments"][:1]
    audit_path.write_text(json.dumps(audit, ensure_ascii=False) + "\n", encoding="utf-8")

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-source-audit"]["meta"]

    assert meta["source_gate"] == "blocked"
    assert meta["source_gate_ready"] is False
    assert meta["source_gate_error"]
    assert meta["joint_answer_dual_source_questions"] == 0
    assert meta["decision_enabled"] is False

    _add_dual_source_audit(data_dir, item_id)
    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-source-audit"]["meta"]

    assert meta["source_gate"] == "pass"
    assert meta["source_gate_ready"] is True
    assert meta["decision_enabled"] is True


def test_stale_owner_packet_is_read_only(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    data_dir, item_id = _make_dataset(
        data_root,
        "v-stale-packet",
        created_at="2026-05-01T00:00:00+08:00",
        status="acceptance_candidate",
    )
    _make_packet(review_root, data_dir, item_id, signed=False)

    manifest_path = data_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-05-02T00:00:00+08:00"
    _write_json(manifest_path, manifest)

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-stale-packet"]["meta"]

    assert meta["mode"] == "stale_packet"
    assert meta["review_enabled"] is False
    assert meta["decision_enabled"] is False
    assert meta["packet_binding_ok"] is False
    assert "manifest" in meta["packet_binding_error"]


def test_wrong_packet_version_is_stale(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    data_dir, item_id = _make_dataset(
        data_root,
        "v-wrong-packet-version",
        created_at="2026-05-03T00:00:00+08:00",
        status="acceptance_candidate",
    )
    packet = _make_packet(review_root, data_dir, item_id, signed=False)
    manifest_path = packet / "packet_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dataset_version"] = "v-other"
    _write_json(manifest_path, manifest)

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-wrong-packet-version"]["meta"]
    assert meta["mode"] == "stale_packet"
    assert meta["review_enabled"] is False
    assert meta["decision_enabled"] is False
    assert "版本" in meta["packet_binding_error"]


def test_packet_file_hash_mismatch_is_stale(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    data_dir, item_id = _make_dataset(
        data_root,
        "v-packet-hash",
        created_at="2026-05-04T00:00:00+08:00",
        status="acceptance_candidate",
    )
    packet = _make_packet(review_root, data_dir, item_id, signed=False)
    signoff_path = packet / "owner_signoff.json"
    signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
    signoff["notes"] = "tampered after packet freeze"
    _write_json(signoff_path, signoff)

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-packet-hash"]["meta"]
    assert meta["mode"] == "stale_packet"
    assert meta["review_enabled"] is False
    assert meta["decision_enabled"] is False
    assert "不匹配" in meta["packet_binding_error"]


def test_missing_signoff_file_is_stale(tmp_path):
    data_root = tmp_path / "data"
    review_root = tmp_path / "review_packets"
    data_dir, item_id = _make_dataset(
        data_root,
        "v-missing-signoff",
        created_at="2026-05-05T00:00:00+08:00",
        status="acceptance_candidate",
    )
    packet = _make_packet(review_root, data_dir, item_id, signed=False)
    (packet / "owner_signoff.json").unlink()

    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        notes_path=None,
    )
    meta = payload["datasets"]["v-missing-signoff"]["meta"]
    assert meta["mode"] == "stale_packet"
    assert meta["review_enabled"] is False
    assert meta["decision_enabled"] is False
    assert "owner_signoff.json" in meta["packet_binding_error"]
