"""Build one owner acceptance dashboard that spans every local dataset version.

The dashboard is intentionally data-driven.  A new package under ``data/`` is
discovered automatically, and an optional owner review packet under
``review_packets/`` enables decisions and sign-off generation for that package.
The generated HTML is self-contained so it can be opened directly with a
``file://`` URL and does not depend on a local HTTP server.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .owner_review import (
    INVALID_IDENTITY_MARKERS,
    OWNER_REVIEW_FIELDS,
    OWNER_REVIEW_VERSION,
    REVIEW_WORKSHEET_NAME,
)
from .scoring import validate_source_audit


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = Path(__file__).resolve().parent / "acceptance_ui"
DEFAULT_UI_DIR = UI_ROOT
DEFAULT_DATA_ROOT = ROOT / "data"
DEFAULT_REVIEW_ROOT = ROOT / "review_packets"
DEFAULT_OUTPUT = ROOT / "reports" / "acceptance" / "index.html"
DEFAULT_NOTES_PATH = DEFAULT_UI_DIR / "acceptance_notes.json"
DASHBOARD_SCHEMA_VERSION = "2.0"

TYPE_LABELS = {
    "single_choice": "单选题",
    "multi_choice": "多选题",
    "fill_blank": "填空题",
    "catalog_subjective": "解答题",
}

STATUS_LABELS = {
    "acceptance_candidate": "候选待签",
    "release_candidate": "发布候选",
    "alpha_internal_pilot": "内部 alpha 试点",
    "archived": "已归档",
    "active": "有效版本",
    "ocr_match_pending_visual": "来源一致 · 待人工视觉",
    "source_and_derivation_confirmed": "来源 + 推导确认",
    "source_and_visual_confirmed": "来源 + 图形确认",
    "external_source_self_conflict": "来源内部冲突，已处置",
    "external_answer_error": "媒体答案有误，已处置",
    "audited_passed": "审计通过",
    "four_pdf_crops_retained_pending_owner_visual_check": "裁图保留 · 待视觉确认",
    "alpha5_pdf_crops_retained_pending_owner_visual_check": "alpha.5 裁图 · 待视觉确认",
    "source_pdf_crops_rebuilt_pending_owner_visual_check": "裁图已重建 · 待视觉确认",
    "recorded_pending_owner_visual_check": "裁图坐标已记录 · 待视觉确认",
    "media_crosschecked_pending_owner_signoff": "媒体交叉核验 · 待签字",
    "media_crosschecked_pending_independent_human_review": "媒体交叉核验 · 待独立复核",
    "independently_derived_pending_owner_signoff": "独立推导 · 待签字",
    "independently_derived_and_source_crosschecked_pending_owner_signoff": "独立推导 + 来源核验 · 待签字",
    "human_cleaned_seed_pending_official_answer_review": "人工清洗种子 · 待答案复核",
    "paper_total_closed_pending_question_level_official_review": "整卷分值闭合 · 待逐题复核",
    "audited_pending_owner_signoff": "审计通过 · 待签字",
    "three_way_audit_passed_pending_owner_visual_signoff": "三方审计通过 · 待视觉签字",
    "three_way_audit_passed_pending_owner_signoff": "三方审计通过 · 待签字",
    "not_required_single_owner_attestation": "单人 owner 签收",
    "missing_answer_key": "缺少答案键",
}

REVIEW_MODE_LABELS = {
    "candidate": "候选待签",
    "signed": "已签收",
    "historical_candidate": "历史候选",
    "archive": "存档快照",
    "stale_packet": "签字包已过期",
}

INTERACTIVE_STATUSES = {"acceptance_candidate", "owner_review_candidate"}


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: %s" % path)
    return value


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSONL at %s:%d: %s" % (path, line_number, exc)) from exc
        if not isinstance(value, dict):
            raise ValueError("JSONL row must be an object at %s:%d" % (path, line_number))
        rows.append(value)
    return rows


def _read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header: %s" % path)
        return list(reader.fieldnames), [dict(row) for row in reader]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonish(value: Any, fallback: Any = None) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    text = str(value).strip()
    if not text:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _as_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _display_answer(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else str(value)


def _human_status(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "未记录"
    if text in STATUS_LABELS:
        return STATUS_LABELS[text]
    return text.replace("_", " ")


def _status_tone(value: Any) -> str:
    text = str(value or "").lower()
    if any(token in text for token in ("fail", "error", "conflict", "invalid", "blocked", "missing")):
        return "fail"
    if any(token in text for token in ("pass", "confirmed", "complete", "signed", "audited")):
        return "pass"
    if any(token in text for token in ("pending", "draft", "provisional", "candidate")):
        return "pending"
    return "neutral"


def _source_page_url(source: Mapping[str, Any], locator: Any) -> str:
    pages = source.get("pages") if isinstance(source.get("pages"), list) else []
    match = re.search(r"page-(\d+)", str(locator or ""))
    page = None
    if match:
        page = next(
            (item for item in pages if item.get("page_index") == int(match.group(1))),
            None,
        )
    if page is None and len(pages) == 1:
        page = pages[0]
    if isinstance(page, Mapping):
        return str(page.get("page_url") or source.get("url") or "")
    return str(source.get("url") or "")


def _source_image_url(source: Mapping[str, Any], locator: Any) -> str:
    pages = source.get("pages") if isinstance(source.get("pages"), list) else []
    match = re.search(r"page-(\d+)", str(locator or ""))
    page = None
    if match:
        page = next(
            (item for item in pages if item.get("page_index") == int(match.group(1))),
            None,
        )
    if page is None and len(pages) == 1:
        page = pages[0]
    if isinstance(page, Mapping):
        return str(page.get("image_url") or "")

    images = source.get("answer_image_urls") if isinstance(source.get("answer_image_urls"), list) else []
    question_match = re.search(r"第\s*(\d+)\s*题", str(locator or ""))
    page_map = source.get("answer_image_pages")
    if question_match and isinstance(page_map, Mapping):
        index = page_map.get(question_match.group(1))
        try:
            position = int(index) - 1
        except (TypeError, ValueError):
            position = -1
        if 0 <= position < len(images):
            item = images[position]
            return str(item.get("image_url", "")) if isinstance(item, Mapping) else str(item)
    if len(images) == 1:
        item = images[0]
        return str(item.get("image_url", "")) if isinstance(item, Mapping) else str(item)
    return ""


def _packet_source(
    row: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    secondary: bool = False,
) -> Dict[str, str]:
    source_id = str(row.get("secondary_source_id" if secondary else "answer_source_id") or "")
    locator = str(row.get("secondary_locator" if secondary else "answer_locator") or "")
    source = sources.get(source_id, {}) if source_id else {}
    if secondary:
        url = row.get("secondary_source_url")
        page_url = row.get("secondary_page_url")
        image_url = row.get("secondary_image_url")
        excerpt = row.get("secondary_answer_excerpt")
    else:
        url = row.get("answer_source_url")
        page_url = row.get("answer_page_url")
        image_url = row.get("answer_image_url")
        excerpt = row.get("answer_excerpt")
    return {
        "id": source_id,
        "publisher": str(source.get("publisher") or source_id or "未记录来源"),
        "url": str(url or source.get("url") or ""),
        "page_url": str(page_url or _source_page_url(source, locator)),
        "image_url": str(image_url or _source_image_url(source, locator)),
        "locator": locator,
        "excerpt": str(excerpt or ""),
    }


def _audit_source(
    audit: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    secondary: bool = False,
) -> Dict[str, str]:
    if secondary:
        source_id = str(audit.get("secondary_source_id") or "")
        locator = str(audit.get("secondary_source_locator") or "")
        excerpt = str(audit.get("secondary_external_canonical") or "")
    else:
        source_id = str(audit.get("external_source_id") or "")
        locator = str(audit.get("external_locator") or "")
        excerpt = str(audit.get("external_excerpt") or "")
    source = sources.get(source_id, {}) if source_id else {}
    return {
        "id": source_id,
        "publisher": str(source.get("publisher") or source_id or "未记录来源"),
        "url": str(source.get("url") or ""),
        "page_url": _source_page_url(source, locator),
        "image_url": _source_image_url(source, locator),
        "locator": locator,
        "excerpt": excerpt,
    }


def _load_notes(path: Optional[Path]) -> Dict[str, Dict[str, str]]:
    if path is None or not path.is_file():
        return {}
    value = _read_json(path)
    notes = value.get("by_question")
    if not isinstance(notes, Mapping):
        return {}
    output: Dict[str, Dict[str, str]] = {}
    for item_id, note in notes.items():
        if not isinstance(note, Mapping):
            continue
        label = str(note.get("label") or "重点复核")
        reason = str(note.get("reason") or "")
        conclusion = str(note.get("answer_conclusion") or "").strip()
        if reason or conclusion:
            entry = {"label": label, "reason": reason}
            if conclusion:
                entry["answer_conclusion"] = conclusion
            output[str(item_id)] = entry
    return output


def _asset_data_uri(
    data_dir: Path,
    relative_path: Any,
    assets: Dict[str, str],
) -> str:
    if not relative_path:
        return ""
    path = Path(str(relative_path))
    if path.is_absolute():
        candidate = path.resolve()
    else:
        candidate = (data_dir / path).resolve()
    try:
        candidate.relative_to(data_dir.resolve())
    except ValueError:
        return ""
    if not candidate.is_file():
        return ""
    digest = _file_sha256(candidate)
    if digest not in assets:
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        assets[digest] = "data:%s;base64,%s" % (
            mime,
            base64.b64encode(candidate.read_bytes()).decode("ascii"),
        )
    return digest


def _paper_meta(
    question: Mapping[str, Any],
    papers: Mapping[str, Mapping[str, Any]],
    fallback: str,
) -> Tuple[str, str, str]:
    paper_id = str(question.get("paper_id") or fallback)
    paper = papers.get(paper_id, {})
    title = str(paper.get("title") or paper_id)
    region = str(paper.get("region") or paper_id)
    return paper_id, title, region


def _asset_meta(data_dir: Path, question: Mapping[str, Any], asset_audit: Mapping[str, Any]) -> Dict[str, Any]:
    source = question.get("source") if isinstance(question.get("source"), Mapping) else {}
    page_number = source.get("page_number") or question.get("page_number")
    bbox = source.get("bbox") or question.get("bbox")
    crop_method = source.get("crop_method") or question.get("crop_method") or ""
    evidence_status = source.get("evidence_status") or question.get("asset_status") or ""
    return {
        "page_number": page_number,
        "bbox": bbox if isinstance(bbox, list) else [],
        "crop_method": str(crop_method),
        "evidence_status": str(evidence_status),
        "source_pdf_sha256": str(source.get("source_pdf_sha256") or ""),
        "before_sha256": str(asset_audit.get("before_sha256") or ""),
        "after_sha256": str(asset_audit.get("after_sha256") or question.get("image_sha256") or ""),
        "audit_status": str(asset_audit.get("status") or ""),
        "audit_decision": str(asset_audit.get("decision") or ""),
        "audit_reason": str(asset_audit.get("reason") or ""),
    }


def _type_label(kind: str) -> str:
    return TYPE_LABELS.get(kind, kind or "未记录题型")


def _row_base(
    *,
    item_id: str,
    paper_id: str,
    paper_title: str,
    paper_region: str,
    display_number: str,
    number: Any,
    kind: str,
    points: int,
    included_in_scoring: bool,
    review_scope: str,
    stem: str,
    options: Any,
    canonical: Any,
    answer_slot_count: int,
    image_dependency: str,
    image_path: str,
    image_sha256: str,
    image_asset: str,
    answer_source: Mapping[str, str],
    secondary_source: Mapping[str, str],
    point_evidence: str,
    point_locator: str,
    answer_status: str,
    independent_audit: Mapping[str, Any],
    asset: Mapping[str, Any],
    source_assessments: Sequence[Mapping[str, Any]],
    rejected_source_ids: Sequence[Any],
    priority: Optional[Mapping[str, str]],
    answer_conclusion: str = "",
    raw: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "item_id": item_id,
        "paper_id": paper_id,
        "paper_title": paper_title,
        "paper_region": paper_region,
        "display_number": display_number,
        "number": number,
        "type": kind,
        "type_label": _type_label(kind),
        "points": int(points or 0),
        "included_in_scoring": bool(included_in_scoring),
        "review_scope": review_scope,
        "stem": stem,
        "options": options if isinstance(options, list) else [],
        "canonical": canonical,
        "canonical_display": _display_answer(canonical),
        "answer_conclusion": answer_conclusion,
        "answer_slot_count": max(1, int(answer_slot_count or 1)),
        "image_dependency": image_dependency or "none",
        "image_path": image_path,
        "image_sha256": image_sha256,
        "image_asset": image_asset,
        "image_data_uri": "",
        "answer_source": dict(answer_source),
        "secondary_source": dict(secondary_source),
        "point_evidence": point_evidence,
        "point_locator": point_locator,
        "answer_status": answer_status,
        "answer_status_label": _human_status(answer_status),
        "independent_audit": dict(independent_audit),
        "asset": dict(asset),
        "source_assessments": list(source_assessments),
        "rejected_source_ids": list(rejected_source_ids),
        "priority": dict(priority) if priority else None,
        "raw": dict(raw),
    }


def _load_packet(review_root: Path, version: str) -> Optional[Path]:
    for suffix in ("-owner-signed", "-owner"):
        candidate = review_root / (version + suffix)
        if (candidate / "owner_review.csv").is_file():
            return candidate
    return None


def _valid_iso_timestamp(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _validate_packet_binding(
    *,
    packet_dir: Path,
    version: str,
    current_manifest_sha256: str,
    review_rows: Sequence[Mapping[str, str]],
) -> Tuple[bool, str, Dict[str, Any], Dict[str, Any], bool]:
    """Return a fail-closed binding result for one owner packet.

    A worksheet without a packet manifest, a packet produced for another data
    version, a changed immutable packet file, or a signoff with only
    ``owner_attested=true`` must never be presented as reviewable or signed.
    """

    packet_manifest_path = packet_dir / "packet_manifest.json"
    signoff_path = packet_dir / "owner_signoff.json"
    if not packet_manifest_path.is_file():
        return False, "签字包缺少 packet_manifest.json", {}, {}, False
    if not signoff_path.is_file():
        return False, "签字包缺少 owner_signoff.json", {}, {}, False
    try:
        packet_manifest = _read_json(packet_manifest_path)
    except (OSError, ValueError) as exc:
        return False, "签字包 manifest 无法读取：%s" % exc, {}, {}, False
    packet_manifest_sha256 = str(packet_manifest.get("dataset_manifest_sha256") or "")
    if packet_manifest.get("dataset_version") != version:
        return (
            False,
            "签字包绑定版本 %s，当前数据包为 %s"
            % (packet_manifest.get("dataset_version"), version),
            packet_manifest,
            {},
            False,
        )
    if packet_manifest.get("packet_version") != OWNER_REVIEW_VERSION:
        return (
            False,
            "签字包版本 %s 与当前复核协议 %s 不一致"
            % (packet_manifest.get("packet_version"), OWNER_REVIEW_VERSION),
            packet_manifest,
            {},
            False,
        )
    if packet_manifest_sha256 != current_manifest_sha256:
        return (
            False,
            "签字包绑定 manifest %s，当前数据包为 %s"
            % (packet_manifest_sha256[:12], current_manifest_sha256[:12]),
            packet_manifest,
            {},
            False,
        )
    try:
        signoff = _read_json(signoff_path)
    except (OSError, ValueError) as exc:
        return False, "签字文件无法读取：%s" % exc, packet_manifest, {}, False

    files = packet_manifest.get("files")
    if not isinstance(files, Mapping):
        return False, "签字包 manifest 缺少文件哈希清单", packet_manifest, signoff, False
    for required_name in (REVIEW_WORKSHEET_NAME, "owner_signoff.json"):
        if required_name not in files:
            return (
                False,
                "签字包 manifest 未覆盖 %s" % required_name,
                packet_manifest,
                signoff,
                False,
            )

    signed_intent = bool(
        signoff.get("owner_attested") is True
        or str(signoff.get("reviewer_id") or "").strip()
        or str(signoff.get("reviewer_name") or "").strip()
        or str(signoff.get("signature") or "").strip()
        or str(signoff.get("reviewed_at") or "").strip()
    )
    mutable_files = {REVIEW_WORKSHEET_NAME, "owner_signoff.json"}
    for name, metadata in files.items():
        relative = str(name or "")
        parts = Path(relative).parts
        if not relative or Path(relative).is_absolute() or ".." in parts:
            return False, "签字包文件清单包含不安全路径：%s" % relative, packet_manifest, signoff, False
        path = packet_dir / relative
        if not path.is_file():
            return False, "签字包文件缺失：%s" % relative, packet_manifest, signoff, False
        if signed_intent and relative in mutable_files:
            continue
        if not isinstance(metadata, Mapping):
            return False, "签字包文件元数据无效：%s" % relative, packet_manifest, signoff, False
        if metadata.get("bytes") != path.stat().st_size:
            return False, "签字包文件字节数不匹配：%s" % relative, packet_manifest, signoff, False
        if metadata.get("sha256") != _file_sha256(path):
            return False, "签字包文件哈希不匹配：%s" % relative, packet_manifest, signoff, False

    required_header = {
        "schema_version": OWNER_REVIEW_VERSION,
        "dataset_version": version,
        "dataset_manifest_sha256": current_manifest_sha256,
        "packet_id": packet_manifest.get("packet_id"),
        "review_worksheet": REVIEW_WORKSHEET_NAME,
        "worksheet_items": len(review_rows),
    }
    for field, expected in required_header.items():
        if signoff.get(field) != expected:
            return (
                False,
                "签字字段 %s 与当前数据包不一致" % field,
                packet_manifest,
                signoff,
                False,
            )

    worksheet_path = packet_dir / REVIEW_WORKSHEET_NAME
    actual_worksheet_sha = _file_sha256(worksheet_path)
    supplied_worksheet_sha = str(signoff.get("review_worksheet_sha256") or "").strip()
    if signed_intent:
        if supplied_worksheet_sha != actual_worksheet_sha:
            return False, "签字文件未绑定当前复核表哈希", packet_manifest, signoff, False
        reviewer_id = str(signoff.get("reviewer_id") or "").strip()
        reviewer_name = str(signoff.get("reviewer_name") or "").strip()
        signature = str(signoff.get("signature") or "").strip()
        if signoff.get("owner_attested") is not True:
            return False, "签字文件 owner_attested 不为 true", packet_manifest, signoff, False
        if not reviewer_id or any(marker in reviewer_id.lower() for marker in INVALID_IDENTITY_MARKERS):
            return False, "签字文件 reviewer_id 为空或为占位值", packet_manifest, signoff, False
        if not reviewer_name or any(marker in reviewer_name.lower() for marker in INVALID_IDENTITY_MARKERS):
            return False, "签字文件 reviewer_name 为空或为占位值", packet_manifest, signoff, False
        if signature != reviewer_name:
            return False, "签字文件的 signature 必须等于 reviewer_name", packet_manifest, signoff, False
        if not _valid_iso_timestamp(signoff.get("reviewed_at")):
            return False, "签字文件 reviewed_at 不是带时区的 ISO 时间", packet_manifest, signoff, False
    elif supplied_worksheet_sha and supplied_worksheet_sha != actual_worksheet_sha:
        return False, "未签字复核表哈希与当前文件不一致", packet_manifest, signoff, False

    return True, "", packet_manifest, signoff, signed_intent


def _packet_rows(
    *,
    data_dir: Path,
    review_root: Path,
    version: str,
    questions: Mapping[str, Mapping[str, Any]],
    catalog: Mapping[str, Mapping[str, Any]],
    answer_keys: Mapping[str, Mapping[str, Any]],
    papers: Mapping[str, Mapping[str, Any]],
    answer_sources: Mapping[str, Mapping[str, Any]],
    answer_audit: Mapping[str, Mapping[str, Any]],
    question_audit: Mapping[str, Mapping[str, Any]],
    asset_audit: Mapping[str, Mapping[str, Any]],
    notes: Mapping[str, Mapping[str, str]],
    assets: Dict[str, str],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    packet_dir = _load_packet(review_root, version)
    if packet_dir is None:
        return None, []
    fields, review_rows = _read_csv(packet_dir / "owner_review.csv")
    current_manifest_sha256 = _file_sha256(data_dir / "manifest.json")
    binding_ok, binding_error, packet_manifest, signoff, signed = _validate_packet_binding(
        packet_dir=packet_dir,
        version=version,
        current_manifest_sha256=current_manifest_sha256,
        review_rows=review_rows,
    )
    packet_manifest_sha256 = str(packet_manifest.get("dataset_manifest_sha256") or "")
    rows: List[Dict[str, Any]] = []
    for review in review_rows:
        item_id = str(review.get("item_id") or "")
        question = questions.get(item_id, {})
        catalog_item = catalog.get(item_id, {})
        item = question or catalog_item
        key = answer_keys.get(item_id, {})
        kind = str(review.get("question_type") or item.get("type") or ("catalog_subjective" if catalog_item else ""))
        included = _as_bool(review.get("included_in_scoring"), item_id in answer_keys)
        paper_id, paper_title, paper_region = _paper_meta(
            {"paper_id": review.get("paper_id") or item.get("paper_id")},
            papers,
            version,
        )
        canonical = _jsonish(review.get("canonical_answer"), key.get("canonical"))
        if canonical in (None, "") and catalog_item:
            canonical = catalog_item.get("reference_answer") or catalog_item.get("reference_answer_seed")
        options = _jsonish(review.get("options"), item.get("options", []))
        image_path = str(item.get("image_path") or "")
        asset = _asset_meta(data_dir, item, asset_audit.get(item_id, {}))
        answer_audit_row = answer_audit.get(item_id, {})
        derivation = question_audit.get(item_id, {})
        if not derivation and catalog_item:
            derivation = {
                "derivation": catalog_item.get("solution_outline", ""),
                "answer_verdict": "catalogued",
                "reviewer": catalog_item.get("review_status", ""),
                "reviewed_at": catalog_item.get("reviewed_at", ""),
            }
        status = str(
            review.get("answer_evidence")
            or key.get("answer_status")
            or catalog_item.get("reference_answer_status")
            or ""
        )
        source = _packet_source(review, answer_sources, False)
        secondary = _packet_source(review, answer_sources, True)
        raw = {field: review.get(field, "") for field in fields}
        row = _row_base(
            item_id=item_id,
            paper_id=paper_id,
            paper_title=paper_title,
            paper_region=paper_region,
            display_number=str(review.get("display_number") or item.get("display_number") or ""),
            number=item.get("number"),
            kind=kind,
            points=int(review.get("points") or item.get("points") or 0),
            included_in_scoring=included,
            review_scope=str(review.get("review_scope") or ("deterministic_objective" if included else "catalog_reference_only")),
            stem=str(review.get("stem") or item.get("stem") or item.get("content") or ""),
            options=options,
            canonical=canonical,
            answer_slot_count=int(item.get("answer_slot_count") or len(key.get("slots") or []) or 1),
            image_dependency=str(review.get("image_dependency") or item.get("asset_dependency") or "none"),
            image_path=image_path,
            image_sha256=str(item.get("image_sha256") or ""),
            image_asset=_asset_data_uri(data_dir, image_path, assets),
            answer_source=source,
            secondary_source=secondary,
            point_evidence=str(review.get("point_evidence") or ""),
            point_locator=str(review.get("point_locator") or ""),
            answer_status=status,
            independent_audit={
                "derivation": derivation.get("derivation", ""),
                "answer_verdict": derivation.get("answer_verdict", ""),
                "stem_verdict": derivation.get("stem_verdict", ""),
                "option_verdict": derivation.get("option_verdict", ""),
                "asset_verdict": derivation.get("asset_verdict", ""),
                "reviewer": derivation.get("reviewer", ""),
                "reviewed_at": derivation.get("reviewed_at", ""),
                "notes": derivation.get("notes"),
            },
            asset=asset,
            source_assessments=answer_audit_row.get("source_assessments", []),
            rejected_source_ids=answer_audit_row.get("rejected_source_ids", []),
            priority=notes.get(item_id),
            answer_conclusion=str((notes.get(item_id) or {}).get("answer_conclusion") or ""),
            raw=raw,
        )
        rows.append(row)
    return {
        "dir": packet_dir,
        "fields": fields,
        "signoff": signoff,
        "packet_manifest": packet_manifest,
        "signed": signed and binding_ok,
        "binding_ok": binding_ok,
        "binding_error": binding_error,
        "manifest_sha256": current_manifest_sha256,
        "packet_manifest_sha256": packet_manifest_sha256,
    }, rows


def _fallback_raw(
    *,
    item_id: str,
    paper_id: str,
    display_number: str,
    kind: str,
    points: int,
    included: bool,
    scope: str,
    stem: str,
    options: Any,
    canonical: Any,
    answer_evidence: str,
    source: Mapping[str, str],
    secondary: Mapping[str, str],
    point_evidence: str,
    point_locator: str,
    image_dependency: str,
) -> Dict[str, Any]:
    return {
        "item_id": item_id,
        "paper_id": paper_id,
        "display_number": display_number,
        "question_type": kind,
        "points": points,
        "included_in_scoring": "true" if included else "false",
        "review_scope": scope,
        "stem": stem,
        "options": json.dumps(options or [], ensure_ascii=False, sort_keys=True),
        "canonical_answer": json.dumps(canonical, ensure_ascii=False, sort_keys=True),
        "answer_evidence": answer_evidence,
        "answer_source_id": source.get("id", ""),
        "answer_source_url": source.get("url", ""),
        "answer_page_url": source.get("page_url", ""),
        "answer_image_url": source.get("image_url", ""),
        "answer_locator": source.get("locator", ""),
        "answer_excerpt": source.get("excerpt", ""),
        "secondary_source_id": secondary.get("id", ""),
        "secondary_source_url": secondary.get("url", ""),
        "secondary_page_url": secondary.get("page_url", ""),
        "secondary_image_url": secondary.get("image_url", ""),
        "secondary_locator": secondary.get("locator", ""),
        "secondary_answer_excerpt": secondary.get("excerpt", ""),
        "point_evidence": point_evidence,
        "point_locator": point_locator,
        "image_dependency": image_dependency,
        "decision": "",
        "reviewed_answer": "",
        "reviewed_points": "",
        "notes": "",
    }


def _data_rows(
    *,
    data_dir: Path,
    questions: Sequence[Mapping[str, Any]],
    answer_keys: Mapping[str, Mapping[str, Any]],
    papers: Mapping[str, Mapping[str, Any]],
    answer_sources: Mapping[str, Mapping[str, Any]],
    answer_audit: Mapping[str, Mapping[str, Any]],
    question_audit: Mapping[str, Mapping[str, Any]],
    point_audit: Mapping[str, Mapping[str, Any]],
    asset_audit: Mapping[str, Mapping[str, Any]],
    notes: Mapping[str, Mapping[str, str]],
    assets: Dict[str, str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for question in questions:
        item_id = str(question.get("id") or "")
        key = answer_keys.get(item_id, {})
        if not item_id:
            continue
        missing_key = not bool(key)
        if not key:
            key = {
                "canonical": None,
                "points": int(question.get("points") or 0),
                "kind": str(question.get("type") or ""),
                "answer_status": "missing_answer_key",
            }
        paper_id, paper_title, paper_region = _paper_meta(question, papers, "")
        audit = answer_audit.get(item_id, {})
        derivation = question_audit.get(item_id, {})
        points = int(question.get("points") or key.get("points") or 0)
        source = _audit_source(audit, answer_sources, False)
        secondary = _audit_source(audit, answer_sources, True)
        status = "missing_answer_key" if missing_key else str(audit.get("status") or key.get("answer_status") or "")
        image_path = str(question.get("image_path") or "")
        row = _row_base(
            item_id=item_id,
            paper_id=paper_id,
            paper_title=paper_title,
            paper_region=paper_region,
            display_number=str(question.get("display_number") or ""),
            number=question.get("number"),
            kind=str(question.get("type") or key.get("kind") or ""),
            points=points,
            included_in_scoring=True,
            review_scope="deterministic_objective",
            stem=str(question.get("stem") or question.get("content") or ""),
            options=question.get("options", []),
            canonical=key.get("canonical"),
            answer_slot_count=int(question.get("answer_slot_count") or len(key.get("slots") or []) or 1),
            image_dependency=str(question.get("asset_dependency") or "none"),
            image_path=image_path,
            image_sha256=str(question.get("image_sha256") or ""),
            image_asset=_asset_data_uri(data_dir, image_path, assets),
            answer_source=source,
            secondary_source=secondary,
            point_evidence=str((point_audit.get(item_id) or {}).get("status") or ""),
            point_locator=str((point_audit.get(item_id) or {}).get("locator") or ""),
            answer_status=status,
            independent_audit={
                "derivation": derivation.get("derivation", ""),
                "answer_verdict": derivation.get("answer_verdict", ""),
                "stem_verdict": derivation.get("stem_verdict", ""),
                "option_verdict": derivation.get("option_verdict", ""),
                "asset_verdict": derivation.get("asset_verdict", ""),
                "reviewer": derivation.get("reviewer", ""),
                "reviewed_at": derivation.get("reviewed_at", ""),
                "notes": derivation.get("notes"),
            },
            asset=_asset_meta(data_dir, question, asset_audit.get(item_id, {})),
            source_assessments=audit.get("source_assessments", []),
            rejected_source_ids=audit.get("rejected_source_ids", []),
            priority=notes.get(item_id),
            answer_conclusion=str((notes.get(item_id) or {}).get("answer_conclusion") or ""),
            raw=_fallback_raw(
                item_id=item_id,
                paper_id=paper_id,
                display_number=str(question.get("display_number") or ""),
                kind=str(question.get("type") or key.get("kind") or ""),
                points=points,
                included=True,
                scope="deterministic_objective",
                stem=str(question.get("stem") or question.get("content") or ""),
                options=question.get("options", []),
                canonical=key.get("canonical"),
                answer_evidence=status,
                source=source,
                secondary=secondary,
                point_evidence=str((point_audit.get(item_id) or {}).get("status") or ""),
                point_locator=str((point_audit.get(item_id) or {}).get("locator") or ""),
                image_dependency=str(question.get("asset_dependency") or "none"),
            ),
        )
        rows.append(row)
    return rows


def _catalog_points(catalog: Sequence[Mapping[str, Any]]) -> int:
    return sum(int(item.get("points") or 0) for item in catalog)


def _version_audits(
    manifest: Mapping[str, Any],
    packet: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    quality = manifest.get("quality") if isinstance(manifest.get("quality"), Mapping) else {}
    formal = quality.get("audits") if isinstance(quality.get("audits"), list) else []
    names = {"A": "题面与裁图", "B": "答案与推导", "C": "工程与判分"}
    audits: List[Dict[str, Any]] = []
    if formal:
        for item in formal:
            audit_id = str(item.get("audit_id") or "?")
            verdict = str(item.get("verdict") or "pending")
            audits.append(
                {
                    "id": audit_id,
                    "title": names.get(audit_id, "审计 " + audit_id),
                    "status": verdict,
                    "label": "通过" if verdict == "pass" else _human_status(verdict),
                    "tone": "pass" if verdict == "pass" else _status_tone(verdict),
                    "detail": "覆盖 %s 题" % item.get("checked_questions", "—"),
                }
            )
    else:
        stages = (
            ("A", "题面与裁图", quality.get("asset_status") or quality.get("page_bbox_evidence")),
            ("B", "答案与推导", quality.get("answer_status") or quality.get("question_audit_status")),
            ("C", "分值与工程", quality.get("point_status") or quality.get("review_status")),
        )
        for audit_id, title, status in stages:
            audits.append(
                {
                    "id": audit_id,
                    "title": title,
                    "status": status,
                    "label": _human_status(status),
                    "tone": _status_tone(status),
                    "detail": "数据包质量记录",
                }
            )
    signed = bool(packet and packet.get("signed"))
    recorded_owner = str(quality.get("owner_signoff") or quality.get("review_status") or "").lower()
    owner_complete = signed or recorded_owner in {"audited_passed", "complete", "signed"}
    owner_status = "complete" if owner_complete else "pending"
    audits.append(
        {
            "id": "OWNER",
            "title": "Owner 最终验收",
            "status": owner_status,
            "label": "已完成" if owner_complete else "待确认",
            "tone": "pass" if owner_complete else "pending",
            "detail": (
                "签字文件已归档"
                if signed
                else "数据包记录为已审计"
                if owner_complete
                else "最终签收由项目 owner 完成"
            ),
        }
    )
    return audits


def _validated_source_audit(
    *,
    data_dir: Path,
    manifest: Mapping[str, Any],
    questions: Sequence[Mapping[str, Any]],
    answer_keys: Mapping[str, Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Re-run the source gate instead of trusting a cached manifest summary."""

    block = manifest.get("source_audit")
    if not isinstance(block, Mapping):
        if (data_dir / "source_audit.jsonl").is_file():
            return {
                "applicable": True,
                "gate": "blocked",
                "evidence_ready": False,
                "questions": len(questions) + len(catalog),
                "stem_dual_source_questions": 0,
                "joint_answer_dual_source_questions": 0,
                "gate_complete_questions": 0,
                "failed_question_ids": [],
                "error": "source_audit.jsonl 存在，但 manifest.source_audit 缺失",
            }
        return {
            "applicable": False,
            "gate": "not_applicable",
            "evidence_ready": True,
            "questions": 0,
            "stem_dual_source_questions": 0,
            "joint_answer_dual_source_questions": 0,
            "gate_complete_questions": 0,
            "failed_question_ids": [],
            "error": "",
        }

    dataset = SimpleNamespace(
        data_dir=data_dir,
        manifest=dict(manifest),
        questions=tuple(questions),
        answer_keys=dict(answer_keys),
        catalog_subjective=tuple(catalog),
    )
    try:
        summary = validate_source_audit(dataset)
    except Exception as exc:  # fail closed: a broken package is never signable
        return {
            "applicable": True,
            "gate": "blocked",
            "evidence_ready": False,
            "policy": dict(block.get("policy") or {}),
            "questions": len(questions) + len(catalog),
            "stem_dual_source_questions": 0,
            "joint_answer_dual_source_questions": 0,
            "gate_complete_questions": 0,
            "failed_question_ids": [],
            "error": str(exc),
        }
    summary["error"] = ""
    return summary


def _version_summary(
    *,
    version: str,
    manifest: Mapping[str, Any],
    papers: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
    packet: Optional[Mapping[str, Any]],
    audits: Sequence[Mapping[str, Any]],
    source_audit_summary: Mapping[str, Any],
    source_count: int,
    asset_count: int,
) -> Dict[str, Any]:
    total_questions = len(rows)
    total_points = sum(int(row.get("points") or 0) for row in rows)
    scored_rows = [row for row in rows if row.get("included_in_scoring")]
    catalog_rows = [row for row in rows if not row.get("included_in_scoring")]
    scored_points = sum(int(row.get("points") or 0) for row in scored_rows)
    catalog_rows_points = sum(int(row.get("points") or 0) for row in catalog_rows)
    catalog_total_points = _catalog_points(catalog)
    packet_binding_ok = bool(packet and packet.get("binding_ok"))
    packet_binding_error = str((packet or {}).get("binding_error") or "")
    signed = bool(packet and packet.get("signed") and packet_binding_ok)
    packet_available = packet is not None
    status = str(manifest.get("status") or "")
    if packet_available and not packet_binding_ok:
        mode = "stale_packet"
    elif signed:
        mode = "signed"
    elif packet_available and status in INTERACTIVE_STATUSES:
        mode = "candidate"
    elif packet_available:
        mode = "historical_candidate"
    else:
        mode = "archive"
    interactive = mode == "candidate"
    source_audit_applicable = bool(source_audit_summary.get("applicable"))
    source_audit_questions = int(source_audit_summary.get("questions") or 0)
    stem_dual_source_questions = int(
        source_audit_summary.get("stem_dual_source_questions") or 0
    )
    joint_answer_dual_source_questions = int(
        source_audit_summary.get("joint_answer_dual_source_questions") or 0
    )
    source_gate_ready = bool(
        source_audit_summary.get("evidence_ready")
        and source_audit_summary.get("gate") == "pass"
    )
    source_gate = str(source_audit_summary.get("gate") or "blocked")
    source_gate_error = str(source_audit_summary.get("error") or "")
    if len(papers) == 1:
        coverage = str(papers[0].get("title") or version)
    else:
        coverage = "%s 套试卷" % len(papers)
    return {
        "id": version,
        "coverage": coverage,
        "created_at": str(manifest.get("created_at") or ""),
        "status": status,
        "status_label": _human_status(status or mode),
        "mode": mode,
        "mode_label": REVIEW_MODE_LABELS[mode],
        "review_enabled": packet_available and packet_binding_ok,
        "decision_enabled": interactive,
        "review_scope": "full_paper" if catalog_rows else "scored_questions",
        "questions": total_questions,
        "points": total_points,
        "scored_questions": len(scored_rows),
        "scored_points": scored_points,
        "catalog_questions_in_review": len(catalog_rows),
        "catalog_points_in_review": catalog_rows_points,
        "catalog_questions_total": len(catalog),
        "catalog_points_total": catalog_total_points,
        "visual_required": sum(1 for row in rows if row.get("image_dependency") == "required"),
        "visual_points": sum(
            int(row.get("points") or 0)
            for row in rows
            if row.get("image_dependency") == "required"
        ),
        "dual_source": joint_answer_dual_source_questions,
        "source_audit_applicable": source_audit_applicable,
        "source_audit_questions": source_audit_questions,
        "stem_dual_source_questions": stem_dual_source_questions,
        "joint_answer_dual_source_questions": joint_answer_dual_source_questions,
        "source_gate": source_gate,
        "source_gate_ready": source_gate_ready,
        "source_audit": dict(source_audit_summary),
        "source_gate_error": source_gate_error,
        "source_gate_label": (
            "题干 %d/%d · 答案 %d/%d"
            % (
                stem_dual_source_questions,
                source_audit_questions,
                joint_answer_dual_source_questions,
                source_audit_questions,
            )
            if source_audit_applicable
            else "历史版本未启用"
        ),
        "source_count": source_count,
        "asset_count": asset_count,
        "manifest_sha256": _file_sha256(Path(str(manifest.get("_manifest_path")))),
        "packet_binding_ok": packet_binding_ok,
        "packet_binding_error": packet_binding_error,
        "packet_manifest_sha256": str((packet or {}).get("packet_manifest_sha256") or ""),
        "audits": list(audits),
        "license": _license(manifest),
    }


def _license(manifest: Mapping[str, Any]) -> str:
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), Mapping) else {}
    return str(
        rights.get("data_license")
        or rights.get("dataset_license")
        or rights.get("license")
        or manifest.get("license")
        or "未声明"
    )


def _build_workspace(
    *,
    data_dir: Path,
    review_root: Path,
    notes: Mapping[str, Mapping[str, str]],
    assets: Dict[str, str],
    generated_at: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    manifest_path = data_dir / "manifest.json"
    manifest = _read_json(manifest_path)
    manifest["_manifest_path"] = str(manifest_path)
    version = str(manifest.get("dataset_version") or data_dir.name)
    questions_list = _read_jsonl(data_dir / "questions.jsonl")
    questions = {str(item.get("id")): item for item in questions_list if item.get("id")}
    catalog_list = _read_jsonl(data_dir / "catalog" / "subjective_catalog.jsonl")
    catalog = {str(item.get("id")): item for item in catalog_list if item.get("id")}
    answer_keys = {
        str(item.get("question_id")): item
        for item in _read_jsonl(data_dir / "answer_keys.jsonl")
        if item.get("question_id")
    }
    papers_list = _read_jsonl(data_dir / "papers.jsonl")
    papers = {str(item.get("id")): item for item in papers_list if item.get("id")}
    answer_sources = {
        str(item.get("id")): item
        for item in _read_jsonl(data_dir / "answer_sources.jsonl")
        if item.get("id")
    }
    answer_audit = {
        str(item.get("question_id")): item
        for item in _read_jsonl(data_dir / "answer_audit.jsonl")
        if item.get("question_id")
    }
    source_audit = {
        str(item.get("question_id")): item
        for item in _read_jsonl(data_dir / "source_audit.jsonl")
        if item.get("question_id")
    }
    question_audit = {
        str(item.get("question_id")): item
        for item in _read_jsonl(data_dir / "question_audit.jsonl")
        if item.get("question_id")
    }
    point_audit = {
        str(item.get("item_id") or item.get("question_id")): item
        for item in _read_jsonl(data_dir / "point_audit.jsonl")
        if item.get("item_id") or item.get("question_id")
    }
    asset_audit = {
        str(item.get("question_id")): item
        for item in _read_jsonl(data_dir / "asset_audit.jsonl")
        if item.get("question_id")
    }
    source_audit_summary = _validated_source_audit(
        data_dir=data_dir,
        manifest=manifest,
        questions=questions_list,
        answer_keys=answer_keys,
        catalog=catalog_list,
    )

    packet, rows = _packet_rows(
        data_dir=data_dir,
        review_root=review_root,
        version=version,
        questions=questions,
        catalog=catalog,
        answer_keys=answer_keys,
        papers=papers,
        answer_sources=answer_sources,
        answer_audit=answer_audit,
        question_audit=question_audit,
        asset_audit=asset_audit,
        notes=notes,
        assets=assets,
    )
    if not rows:
        rows = _data_rows(
            data_dir=data_dir,
            questions=questions_list,
            answer_keys=answer_keys,
            papers=papers,
            answer_sources=answer_sources,
            answer_audit=answer_audit,
            question_audit=question_audit,
            point_audit=point_audit,
            asset_audit=asset_audit,
            notes=notes,
            assets=assets,
        )
    for row in rows:
        row["source_audit"] = source_audit.get(str(row.get("item_id")), {})
    rows.sort(key=lambda row: (str(row.get("paper_id") or ""), int(row.get("number") or 0), str(row.get("item_id") or "")))

    fields = packet.get("fields") if packet else list(OWNER_REVIEW_FIELDS)
    signoff_template = packet.get("signoff") if packet else {}
    audits = _version_audits(manifest, packet)
    source_ids = set(str(source_id) for source_id in answer_sources)
    for row in rows:
        for key in ("answer_source", "secondary_source"):
            source_id = (row.get(key) or {}).get("id")
            if source_id:
                source_ids.add(str(source_id))
        for assessment in row.get("source_assessments") or []:
            source_id = assessment.get("source_id") if isinstance(assessment, Mapping) else None
            if source_id:
                source_ids.add(str(source_id))
    asset_ids = {row.get("image_asset") for row in rows if row.get("image_asset")}
    summary = _version_summary(
        version=version,
        manifest=manifest,
        papers=papers_list,
        rows=rows,
        catalog=catalog_list,
        packet=packet,
        audits=audits,
        source_audit_summary=source_audit_summary,
        source_count=len(source_ids),
        asset_count=len(asset_ids),
    )
    summary["manifest_sha256"] = _file_sha256(manifest_path)
    summary["packet_id"] = str((packet or {}).get("packet_manifest", {}).get("packet_id") or "")
    summary["packet_version"] = str((packet or {}).get("packet_manifest", {}).get("packet_version") or "")
    summary["signed_reviewer"] = str((packet or {}).get("signoff", {}).get("reviewer_name") or "")
    summary["signed_at"] = str((packet or {}).get("signoff", {}).get("reviewed_at") or "")
    summary["packet_binding_ok"] = bool((packet or {}).get("binding_ok"))
    summary["packet_binding_error"] = str((packet or {}).get("binding_error") or "")
    summary["packet_manifest_sha256"] = str((packet or {}).get("packet_manifest_sha256") or "")
    summary["subtitle"] = "%s · %s · %s" % (version, summary["coverage"], summary["mode_label"])

    paper_meta = {str(paper.get("id") or ""): paper for paper in papers_list}
    by_paper: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        paper_id = str(row.get("paper_id") or "")
        paper = paper_meta.get(paper_id, {})
        bucket = by_paper.setdefault(
            paper_id,
            {
                "title": paper.get("title") or row.get("paper_title") or paper_id,
                "region": paper.get("region") or row.get("paper_region") or paper_id,
                "questions": 0,
                "points": 0,
            },
        )
        bucket["questions"] += 1
        bucket["points"] += int(row.get("points") or 0)

    quality = manifest.get("quality") if isinstance(manifest.get("quality"), Mapping) else {}
    boundary = (
        "本页复核 %d 题 / %d 分；自动评分 %d 题 / %d 分；主观题目录 %d 题 / %d 分。"
        % (
            summary["questions"],
            summary["points"],
            summary["scored_questions"],
            summary["scored_points"],
            summary["catalog_questions_total"],
            summary["catalog_points_total"],
        )
    )
    if summary["catalog_questions_in_review"]:
        boundary += " 本页包含 %d 道主观题，按题面、参考答案和解答要点复核，不进入自动评分分母。" % summary[
            "catalog_questions_in_review"
        ]
    else:
        boundary += " 本页只逐题判定自动评分题；解答题目录仅做统计展示。"
    meta = dict(summary)
    meta.update(
        {
            "dataset": manifest.get("dataset") or "Gaokao-Bench",
            "dataset_version": version,
            "generated_at": generated_at,
            "total_questions": summary["questions"],
            "total_points": summary["points"],
            "asset_audit_rows": summary["asset_count"],
            "asset_crop_method": str(quality.get("asset_crop_method") or quality.get("crop_method") or ""),
            "asset_status": str(quality.get("asset_status") or ""),
            "status": summary["status"],
            "quality": quality,
            "by_paper": by_paper,
            "priority_count": sum(1 for row in rows if row.get("priority")),
            "single_choice": sum(1 for row in rows if row.get("type") == "single_choice"),
            "multi_choice": sum(1 for row in rows if row.get("type") == "multi_choice"),
            "fill_blank": sum(1 for row in rows if row.get("type") == "fill_blank"),
            "catalog_subjective": sum(1 for row in rows if row.get("type") == "catalog_subjective"),
            "seeded_corrections": [
                str(row.get("item_id")) for row in rows if str((row.get("raw") or {}).get("decision") or "").lower() == "correct"
            ],
            "seeded_disputes": [
                str(row.get("item_id")) for row in rows if str((row.get("raw") or {}).get("decision") or "").lower() == "dispute"
            ],
            "boundary": boundary,
            "visual_explanation": "视觉必需 %d 题 / %d 分；判定依据来自逐题 image_dependency=required。" % (
                summary["visual_required"],
                summary["visual_points"],
            ),
            "source_note": (
                "每题必须同时具备至少两个独立题干来源和两个独立“题干+答案”来源；"
                "仅有答案的页面不进入硬门槛。来源为媒体/文档交叉核验，不等同于考试院官方答案。"
                if summary["source_audit_applicable"]
                else "历史版本按当时的来源记录展示；来源为媒体/文档交叉核验，不等同于考试院官方答案。"
            ),
            "source_audit": summary["source_audit"],
            "license": summary["license"],
            "review_scope_label": "全卷复核" if summary["review_scope"] == "full_paper" else "自动评分题复核",
        }
    )
    return meta, {
        "meta": meta,
        "rows": rows,
        "export_fields": fields,
        "signoff_template": signoff_template,
        "packet_dir": str(packet.get("dir")) if packet else "",
        "packet_manifest": (packet or {}).get("packet_manifest", {}),
    }


def build_acceptance_payload(
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    review_root: Path = DEFAULT_REVIEW_ROOT,
    versions: Optional[Sequence[str]] = None,
    notes_path: Optional[Path] = DEFAULT_NOTES_PATH,
    default_version: Optional[str] = None,
) -> Dict[str, Any]:
    data_root = data_root.resolve()
    review_root = review_root.resolve()
    if not data_root.is_dir():
        raise ValueError("data root not found: %s" % data_root)

    requested = set(versions or [])
    discovered: List[Path] = []
    seen_versions: Dict[str, Path] = {}
    for child in sorted(data_root.iterdir()):
        if not child.is_dir() or not (child / "manifest.json").is_file():
            continue
        manifest = _read_json(child / "manifest.json")
        version = str(manifest.get("dataset_version") or child.name)
        if version in seen_versions:
            raise ValueError(
                "duplicate dataset_version %s in %s and %s"
                % (version, seen_versions[version], child)
            )
        if requested and version not in requested:
            continue
        seen_versions[version] = child
        discovered.append(child)
    if requested and len(discovered) != len(requested):
        found = {
            str(_read_json(child / "manifest.json").get("dataset_version") or child.name)
            for child in discovered
        }
        missing = sorted(requested - found)
        if missing:
            raise ValueError("requested data versions not found: %s" % ", ".join(missing))
    if not discovered:
        raise ValueError("no dataset packages found under %s" % data_root)

    notes = _load_notes(notes_path)
    generated_at = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    assets: Dict[str, str] = {}
    summaries: List[Dict[str, Any]] = []
    workspaces: Dict[str, Any] = {}
    for data_dir in discovered:
        meta, workspace = _build_workspace(
            data_dir=data_dir,
            review_root=review_root,
            notes=notes,
            assets=assets,
            generated_at=generated_at,
        )
        version = meta["dataset_version"]
        workspaces[version] = workspace
        summaries.append(
            {
                "id": version,
                "coverage": meta["coverage"],
                "subtitle": meta["subtitle"],
                "created_at": meta["created_at"],
                "status": meta["status"],
                "status_label": meta["status_label"],
                "mode": meta["mode"],
                "mode_label": meta["mode_label"],
                "review_enabled": meta["review_enabled"],
                "decision_enabled": meta["decision_enabled"],
                "questions": meta["questions"],
                "points": meta["points"],
                "scored_questions": meta["scored_questions"],
                "scored_points": meta["scored_points"],
                "catalog_questions_in_review": meta["catalog_questions_in_review"],
                "catalog_points_in_review": meta["catalog_points_in_review"],
                "catalog_questions_total": meta["catalog_questions_total"],
                "catalog_points_total": meta["catalog_points_total"],
                "visual_required": meta["visual_required"],
                "visual_points": meta["visual_points"],
                "dual_source": meta["dual_source"],
                "source_audit_applicable": meta["source_audit_applicable"],
                "source_audit_questions": meta["source_audit_questions"],
                "stem_dual_source_questions": meta["stem_dual_source_questions"],
                "joint_answer_dual_source_questions": meta[
                    "joint_answer_dual_source_questions"
                ],
                "source_gate": meta["source_gate"],
                "source_gate_ready": meta["source_gate_ready"],
                "source_gate_error": meta["source_gate_error"],
                "source_gate_label": meta["source_gate_label"],
                "source_count": meta["source_count"],
                "asset_count": meta["asset_count"],
                "manifest_sha256": meta["manifest_sha256"],
                "packet_binding_ok": meta["packet_binding_ok"],
                "packet_binding_error": meta["packet_binding_error"],
                "audits": meta["audits"],
                "license": meta["license"],
                "packet_id": meta["packet_id"],
                "signed_reviewer": meta["signed_reviewer"],
                "signed_at": meta["signed_at"],
            }
        )

    summaries.sort(key=lambda item: (item.get("created_at") or "", item.get("id") or ""), reverse=True)
    summary_by_id = {item["id"]: item for item in summaries}
    if default_version:
        if default_version not in summary_by_id:
            raise ValueError("default version is not in the built catalog: %s" % default_version)
        selected = default_version
    else:
        selected = summaries[0]["id"]

    return {
        "schema_version": DASHBOARD_SCHEMA_VERSION,
        "generated_at": generated_at,
        "default_version": selected,
        "versions": summaries,
        "datasets": workspaces,
        "assets": assets,
        "builder": {
            "data_root": str(data_root),
            "review_root": str(review_root),
            "notes_path": str(notes_path) if notes_path else "",
        },
    }


def build_acceptance_page(
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    review_root: Path = DEFAULT_REVIEW_ROOT,
    ui_dir: Path = DEFAULT_UI_DIR,
    output: Path = DEFAULT_OUTPUT,
    versions: Optional[Sequence[str]] = None,
    notes_path: Optional[Path] = DEFAULT_NOTES_PATH,
    default_version: Optional[str] = None,
) -> Dict[str, Any]:
    payload = build_acceptance_payload(
        data_root=data_root,
        review_root=review_root,
        versions=versions,
        notes_path=notes_path,
        default_version=default_version,
    )
    template = (ui_dir / "owner_review_dashboard.html").read_text(encoding="utf-8")
    styles = (ui_dir / "owner_review_dashboard.css").read_text(encoding="utf-8")
    script = (ui_dir / "owner_review_dashboard.js").read_text(encoding="utf-8")
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data_json = data_json.replace("</", "<\\/")
    document = (
        template.replace("{{STYLES}}", styles)
        .replace("{{SCRIPT}}", script)
        .replace("{{DATA_JSON}}", data_json)
    )
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(document, encoding="utf-8")
    return {
        "output": str(output),
        "bytes": output.stat().st_size,
        "versions": len(payload["versions"]),
        "default_version": payload["default_version"],
        "questions": sum(item["questions"] for item in payload["versions"]),
        "assets": len(payload["assets"]),
    }


__all__ = [
    "DEFAULT_DATA_ROOT",
    "DEFAULT_OUTPUT",
    "DEFAULT_REVIEW_ROOT",
    "DEFAULT_UI_DIR",
    "DASHBOARD_SCHEMA_VERSION",
    "build_acceptance_page",
    "build_acceptance_payload",
]
