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
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs" / "2026-math-n1.json"
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
        if not candidate_manifest_sha256 or report.get("dataset_manifest_sha256") != candidate_manifest_sha256:
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
    source = spec["source"]
    paper_spec = spec["paper"]
    version = str(spec["dataset_version"])
    out_dir = Path(args.out).resolve()
    revision = source_revision()
    spec_path = Path(args.spec).resolve()
    if out_dir == ROOT or out_dir == Path.home():
        raise ValueError("refusing unsafe output directory")
    pdf_path = resolve_pdf(args.pdf, Path(args.cache_dir).resolve(), source)
    prior_manifest = out_dir / "manifest.json"
    prior_manifest_sha256 = sha256(prior_manifest) if prior_manifest.is_file() else None
    audit_reports = validate_audit_reports(
        [Path(value).resolve() for value in args.audit_report], spec, prior_manifest_sha256
    )
    audited = bool(audit_reports)

    temporary = out_dir.with_name(out_dir.name + ".building")
    if temporary.exists():
        shutil.rmtree(temporary)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    temporary.mkdir(parents=True)
    assets_dir = temporary / "assets" / paper_spec["id"]
    catalog_dir = temporary / "catalog"
    examples_dir = temporary / "examples"
    for directory in (assets_dir, catalog_dir, examples_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if audit_reports:
        audit_report_dir = temporary / "audit_reports"
        audit_report_dir.mkdir(parents=True)
        for report in audit_reports:
            source_path = Path(report["report_path"])
            shutil.copyfile(
                source_path,
                audit_report_dir / ("audit-%s.json" % report["audit_id"].lower()),
            )
            markdown_path = source_path.with_suffix(".md")
            if markdown_path.is_file():
                shutil.copyfile(
                    markdown_path,
                    audit_report_dir / ("audit-%s.md" % report["audit_id"].lower()),
                )

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
    answer_sources = [
        {
            **row,
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
        }
        for row in spec["answer_sources"]
    ]
    answer_audit: List[Dict[str, Any]] = []
    question_audit: List[Dict[str, Any]] = []
    point_audit: List[Dict[str, Any]] = []
    asset_audit: List[Dict[str, Any]] = []
    for item, key in zip(spec["objective"], answer_keys):
        qid = item["id"]
        supporting = ["src-bendibao-2026-n1-answers"]
        if int(item["number"]) <= 5:
            supporting.insert(0, "src-zizzs-2026-n1-answers")
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
                "external_source_id": "src-gk100-2026-n1-answers",
                "external_locator": "完整解析图；第%d题" % int(item["number"]),
                "external_excerpt": "【答案】%s" % json.dumps(item["canonical"], ensure_ascii=False),
                "secondary_source_id": "src-sohu-2026-n1-answers",
                "secondary_external_canonical": item["canonical"],
                "secondary_source_locator": "教师研判参考答案；第%d题" % int(item["number"]),
                "supporting_source_ids": supporting,
                "rejected_source_ids": [],
                "match_method": "explicit_answer_and_independent_derivation",
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
                "source_ids": [source["id"], "src-sohu-2026-n1-answers"],
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
    write_jsonl(temporary / "sources.jsonl", [
        {
            "id": source["id"],
            "paper_id": paper_id,
            "publisher": source["publisher"],
            "url": source["url"],
            "pdf_sha256": source["sha256"],
            "page_count": int(source["page_count"]),
            "source_type": "exam_paper_repository",
            "retrieved_at": source["retrieved_at"],
            "rights_status": "cc_by_4_0_public_release",
            "redistribution": "cleared_with_attribution",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "rights_basis": "project_owner_elected_cc_by_4_0_for_the_complete_data_package",
            "notes": "Text-native exam paper; the source file contains no answer key.",
        }
    ])
    write_jsonl(temporary / "answer_sources.jsonl", answer_sources)
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

    audit_summary = "三方独立审计已通过，等待 owner 最终逐题验收。" if audited else "三方独立审计尚未完成。"
    (temporary / "DATA_CARD.md").write_text(
        "# Gaokao-Bench 2026 全国卷 I 数学（验收候选）\n\n"
        "- 范围：2026 年全国卷 I 数学全套 19 题，150 分。\n"
        "- 确定性自动评分：选择/填空 14 题，73 分。\n"
        "- 解答题：5 题，77 分，已收录题面、参考答案和解答要点，暂不进入自动评分分母。\n"
        "- 图形依赖：客观题 0 题；解答题第 15 题需要随附立体几何图。\n"
        "- 来源：deekur/gaokaomath 冻结 PDF；答案交叉核验使用高考100、深圳本地宝、自主选拔在线和搜狐转载的教师研判版。\n"
        "- 署名：完整来源清单与第三方声明见 `THIRD_PARTY_NOTICES.md`、`SOURCE_REGISTER.md` 和来源 JSONL。\n"
        "- 许可：整个数据包采用 CC BY 4.0，署名 Gaokao-Bench 与准确数据版本即可再分发和改编。\n"
        f"- 状态：`acceptance_candidate`，{audit_summary}未发布。\n",
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
        "| ID | Publisher | Role | URL |\n"
        "|---|---|---|---|\n"
        f"| `{source['id']}` | {source['publisher']} | Frozen source PDF, 3 pages, no answers | `{source['url']}` |\n"
        + "\n".join(
            "| `%s` | %s | %s | `%s` |"
            % (row["id"], row["publisher"], row["source_type"], row["url"])
            for row in answer_sources
        )
        + "\n\nSource PDF SHA-256: `%s`.\n\nAttribution is consolidated in "
        "`THIRD_PARTY_NOTICES.md`. All package material is released under CC BY 4.0 "
        "with attribution to Gaokao-Bench and this exact dataset version.\n" % source["sha256"],
        encoding="utf-8",
    )
    (temporary / "RELEASE_NOTES.md").write_text(
        "# 2026 National I mathematics acceptance candidate\n\n"
        "Full 19-question catalogue; 14 objective questions / 73 points are "
        "deterministically scored, and 5 subjective questions / 77 points are "
        "catalogued with reference answers and solution outlines. Not published; "
        "owner acceptance remains pending.\n",
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
        "python3 -m pip install -e '.[dev]'\n"
        "python3 scripts/build_2026_n1.py --spec specs/2026-math-n1.json --out data/%s\n"
        "python3 scripts/validate.py --data-dir data/%s\n"
        "python3 -m gaokao_bench.cli evaluate --data-dir data/%s --answers data/%s/examples/gold.json\n"
        "python3 -m pytest -q\n"
        "```\n\n"
        "The builder verifies source PDF SHA-256 `%s`, re-renders all 19 crops from "
        "explicit page bounding boxes, and writes a 73/73 objective gold result. "
        "Builder SHA-256: `%s`; spec SHA-256: `%s`.\n"
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
            sha256(Path(__file__).resolve()),
            sha256(spec_path),
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
        "quality": {
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
            "audits": audit_reports,
        },
        "scoring": {
            "contract": "single_primary_metric_v1",
            "primary_metric": "score.weighted_points",
            "unit": "ratio",
            "metric_version": "1.0",
            "partial_credit": False,
            "primary_task_types": list(spec["benchmark_spec"]["scoring"]["primary_task_types"]),
            "catalog_task_types": list(spec["benchmark_spec"]["scoring"]["catalog_task_types"]),
            "credit_policy": spec["benchmark_spec"]["scoring"]["credit_policy"],
            "process_policy": spec["benchmark_spec"]["scoring"]["process_policy"],
            "profile": "strict-source-audited-points-all-or-nothing",
            "missing_or_invalid_is_zero": True,
            "diagnostic_metrics": list(spec["benchmark_spec"]["scoring"]["diagnostic_metrics"]),
        },
        "compatibility": {
            "dataset": version,
            "benchmark_spec": spec["benchmark_spec"]["version"],
            "engine": "0.1.7",
        },
        "versions": {
            "dataset": version,
            "benchmark_spec": spec["benchmark_spec"]["version"],
            "engine": "0.1.7",
        },
        "build_provenance": {
            "repository": SOURCE_REPOSITORY,
            "revision": revision,
            "builder_path": "scripts/build_2026_n1.py",
            "builder_sha256": sha256(Path(__file__).resolve()),
            "spec_path": "specs/2026-math-n1.json",
            "spec_sha256": sha256(spec_path),
            "reproduction": "REPRODUCE.md",
            "publication_gate": "owner_acceptance_then_push_and_public_rebuild",
        },
        "audit": {
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
    parser.add_argument("--out", default=str(ROOT / "data" / "0.1.0-alpha.2026-n1"))
    parser.add_argument("--created-at", default="2026-11-11T18:00:00+08:00")
    parser.add_argument("--audit-report", action="append", default=[], help="three JSON audit reports: A, B, C")
    args = parser.parse_args()
    manifest = build(args)
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
