import base64
import csv
import json
from pathlib import Path

from gaokao_bench import cli
from gaokao_bench.acceptance_dashboard import build_acceptance_page, build_acceptance_payload
from gaokao_bench.owner_review import OWNER_REVIEW_FIELDS


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
    _write_json(
        packet / "owner_signoff.json",
        {
            "owner_attested": signed,
            "reviewer_name": "测试负责人" if signed else "",
            "reviewed_at": "2026-01-02T00:00:00+08:00" if signed else "",
        },
    )
    _write_json(
        packet / "packet_manifest.json",
        {"packet_id": packet.name, "packet_version": "1.0"},
    )
    return packet


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
