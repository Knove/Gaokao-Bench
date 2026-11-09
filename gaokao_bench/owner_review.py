"""Owner review and single-signoff workflow for Gaokao-Bench.

This workflow records the project owner's acceptance of a frozen dataset.  It
does not represent independent human review: that status remains pending until
two different reviewers complete the separate review packet.

Exports contain a blank worksheet and a blank signature template.  Imports are
read-only with respect to the data package and only emit an attestation report
plus proposed revisions.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .scoring import Dataset, load_dataset, validate_dataset

OWNER_REVIEW_VERSION = "1.2"
REVIEW_WORKSHEET_NAME = "owner_review.csv"
SIGNOFF_NAME = "owner_signoff.json"
OWNER_REVIEW_FIELDS = (
    "item_id",
    "paper_id",
    "display_number",
    "question_type",
    "points",
    "stem",
    "options",
    "canonical_answer",
    "answer_evidence",
    "answer_source_id",
    "answer_source_url",
    "answer_page_url",
    "answer_image_url",
    "answer_locator",
    "answer_excerpt",
    "secondary_source_id",
    "secondary_source_url",
    "secondary_page_url",
    "secondary_image_url",
    "secondary_locator",
    "secondary_answer_excerpt",
    "point_evidence",
    "point_locator",
    "image_dependency",
    "decision",
    "reviewed_answer",
    "reviewed_points",
    "notes",
)
ALLOWED_DECISIONS = {"agree", "correct", "dispute"}
INVALID_IDENTITY_MARKERS = (
    "pending",
    "codex",
    "agent",
    "provisional",
    "automatic",
    "automated",
    "tbd",
    "todo",
)


class OwnerReviewValidationError(ValueError):
    """Raised when the owner review worksheet or signature is invalid."""


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OwnerReviewValidationError("invalid JSONL at %s:%d: %s" % (path, line_number, exc)) from exc
            if not isinstance(row, dict):
                raise OwnerReviewValidationError("JSONL row must be an object at %s:%d" % (path, line_number))
            rows.append(row)
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _one_line(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source_page(source: Mapping[str, Any], locator: Any) -> Optional[Mapping[str, Any]]:
    match = re.search(r"page-(\d+)", str(locator or ""))
    if not match:
        return None
    return next(
        (item for item in source.get("pages", []) if item.get("page_index") == int(match.group(1))),
        None,
    )


def _source_page_url(source: Mapping[str, Any], locator: Any) -> str:
    page = _source_page(source, locator)
    pages = source.get("pages", [])
    if page is None and len(pages) == 1:
        page = pages[0]
    if page is not None:
        return str(page.get("page_url", ""))
    return str(source.get("url", ""))


def _source_image_url(source: Mapping[str, Any], locator: Any) -> str:
    page = _source_page(source, locator)
    pages = source.get("pages", [])
    if page is None and len(pages) == 1:
        page = pages[0]
    return str(page.get("image_url", "")) if page else ""


def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise OwnerReviewValidationError("owner review worksheet has no header: %s" % path)
        return list(reader.fieldnames), [dict(row) for row in reader]


def _review_instructions(dataset: Dataset, packet_id: str) -> str:
    return """# Gaokao-Bench owner 复核包

> Packet: `{packet_id}`  
> Dataset: `{version}`  
> 数据 manifest SHA-256: `{manifest_sha}`  
> 范围：{questions} 道选择题/填空题，{points} 分

## 怎么复核

1. 打开 `owner_review.csv`，逐行核对题干、选项、答案、分值和两条来源定位；
2. 每行 `decision` 只填 `agree`、`correct` 或 `dispute`；
3. `correct` 时填写 `reviewed_answer` 和/或 `reviewed_points`，并在 `notes` 写明依据；
4. `dispute` 时必须在 `notes` 写明争议点；
5. 不要修改 `item_id`、`canonical_answer`、`points` 等基线列；
6. 在 `owner_signoff.json` 中填写真实姓名、标识、签名、带时区时间和
   `owner_attested=true`；
7. 签名必须与姓名完全一致，时间格式示例：`2026-11-05T14:30:00+08:00`。

多空答案写 JSON 数组，例如 `["1", "15"]`；单选题写 `C`。
主来源列以 `answer_*` 开头，独立第二来源列以 `secondary_*` 开头。
来源可能是分页答案图片、题库页面或文档预览；网页正文可能搜不到题号。请优先打开
`answer_image_url` / `secondary_page_url` 指向的证据页，再用
`answer_locator` / `secondary_locator` 核对。`GB25-N1-Q13` 另在验收台中逐条
展示 `source_assessments`：题干不匹配或题干不可见的来源必须显示为 rejected。
如使用浏览器验收台，打开 `reports/acceptance/{version}/index.html`，逐题点击
“同意 / 纠错 / 争议”，最后下载 CSV 与签字 JSON；页面不会代填身份。

## 边界

- 这是 **项目 owner 单人验收签字**，不是两名独立审校者的双人复核；
- `0.1.0-alpha.3-owner` 包含一个已确认错误答案，**不得签字**；
- `0.1.0-alpha.4-owner` 的题目裁图普遍错位或缺失，**不得签字**；本包为 `{version}`；
- `0.1.0-alpha.5-owner` 的 Q13 引用了缺少正项条件的变体题面，**不得签字**；本包为 `{version}`；
- 导入器只生成报告和变更建议，不会自动改写数据；
- 如果签字时有 `correct`，必须发布新的不可变数据版本；
- 本包仅供内部验收，不解决第三方试题素材的再分发授权。

## 提交

填好 CSV 与 JSON 后运行：

```bash
python3 scripts/import_owner_signoff.py \\
  --data-dir data/{version} \\
  --review-csv review_packets/{version}-owner/{worksheet} \\
  --signoff review_packets/{version}-owner/{signoff} \\
  --output reports/{version}-owner-signoff.json
```
""".format(
        packet_id=packet_id,
        version=dataset.manifest["dataset_version"],
        manifest_sha=_file_sha256(dataset.data_dir / "manifest.json"),
        questions=len(dataset.questions),
        points=sum(int(question["points"]) for question in dataset.questions),
        worksheet=REVIEW_WORKSHEET_NAME,
        signoff=SIGNOFF_NAME,
    )


def _review_notes(dataset: Dataset) -> str:
    version = dataset.manifest["dataset_version"]
    asset_count = len(_read_jsonl(dataset.data_dir / "asset_audit.jsonl"))
    return """# {version} owner 复核说明

本包包含 {asset_count} 条素材审计、74 条答案证据和 74 条分值证据。

## 复核要求

1. 逐题对照题干、选项、canonical answer、主来源和第二来源；
2. 对 `external_answer_error`、`external_source_self_conflict` 和
   `ocr_match_pending_visual` 状态重点复核；
3. 对 `source_assessments` 中的 `stem_match` 逐条复核：缺条件或题干不可见的来源不得支持答案；
4. 对视觉必需题检查裁图是否包含完整图形、单位和标注；
5. 对来源冲突只接受可复算推导或明确选项映射，不能直接抄来源字母；
6. 发现修正或争议时填写 `correct`/`dispute`，不得静默覆盖 canonical。

具体的逐题争议说明由数据负责人作为受控复核附件提供，不硬编码在
公开软件代码中。证据来源为非官方交叉核验，不代表考试院官方答案。
""".format(version=version, asset_count=asset_count)


def _release_review_notes_path(dataset: Dataset) -> Path:
    root = dataset.data_dir.parent.parent if dataset.data_dir.parent.name == "data" else dataset.data_dir.parent
    return root / "release" / str(dataset.manifest["dataset_version"]) / "owner-review-notes.md"


def export_owner_review(
    data_dir: Union[Path, str],
    out_dir: Union[Path, str],
    *,
    created_at: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Write a blank owner worksheet and signoff template."""

    dataset = load_dataset(data_dir)
    validate_dataset(dataset)
    destination = Path(out_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        if not force:
            raise OwnerReviewValidationError("output directory is not empty; pass force=True to replace it: %s" % destination)
        if (
            destination == dataset.data_dir
            or dataset.data_dir in destination.parents
            or destination in dataset.data_dir.parents
        ):
            raise OwnerReviewValidationError("refusing to replace the data package or one of its parent directories")
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    created_at = created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    manifest_sha = _file_sha256(dataset.data_dir / "manifest.json")
    packet_id = "GB-OWNER-%s-%s" % (dataset.manifest["dataset_version"], manifest_sha[:12])
    answer_audit = {row["question_id"]: row for row in _read_jsonl(dataset.data_dir / "answer_audit.jsonl")}
    point_audit = {row["item_id"]: row for row in _read_jsonl(dataset.data_dir / "point_audit.jsonl")}
    answer_sources = {row["id"]: row for row in _read_jsonl(dataset.data_dir / "answer_sources.jsonl")}

    rows: List[Dict[str, Any]] = []
    for question in dataset.questions:
        qid = question["id"]
        key = dataset.answer_keys[qid]
        answer_evidence = answer_audit[qid]
        point_evidence = point_audit[qid]
        source = answer_sources[answer_evidence["external_source_id"]]
        page_url = _source_page_url(source, answer_evidence.get("external_locator"))
        image_url = _source_image_url(source, answer_evidence.get("external_locator"))
        secondary_source_id = answer_evidence.get("secondary_source_id") or ""
        secondary_source = answer_sources.get(secondary_source_id, {}) if secondary_source_id else {}
        secondary_page_url = _source_page_url(secondary_source, answer_evidence.get("secondary_source_locator"))
        secondary_image_url = _source_image_url(secondary_source, answer_evidence.get("secondary_source_locator"))
        secondary_answer = answer_evidence.get("secondary_external_canonical")
        rows.append(
            {
                "item_id": qid,
                "paper_id": question["paper_id"],
                "display_number": question.get("display_number", ""),
                "question_type": question["type"],
                "points": int(question["points"]),
                "stem": _one_line(question.get("stem") or question.get("content")),
                "options": _json_text(question.get("options", [])),
                "canonical_answer": _json_text(key.get("canonical")),
                "answer_evidence": answer_evidence.get("status", ""),
                "answer_source_id": answer_evidence.get("external_source_id", ""),
                "answer_source_url": source.get("url", ""),
                "answer_page_url": page_url,
                "answer_image_url": image_url,
                "answer_locator": answer_evidence.get("external_locator", ""),
                "answer_excerpt": _one_line(answer_evidence.get("external_excerpt")),
                "secondary_source_id": secondary_source_id,
                "secondary_source_url": secondary_source.get("url", ""),
                "secondary_page_url": secondary_page_url,
                "secondary_image_url": secondary_image_url,
                "secondary_locator": answer_evidence.get("secondary_source_locator", ""),
                "secondary_answer_excerpt": _one_line(
                    secondary_answer
                    if secondary_answer is not None
                    else "该题未配置独立第二来源，请以主来源和独立推导复核"
                ),
                "point_evidence": point_evidence.get("status", ""),
                "point_locator": point_evidence.get("locator", ""),
                "image_dependency": question.get("asset_dependency", ""),
                "decision": "",
                "reviewed_answer": "",
                "reviewed_points": "",
                "notes": "",
            }
        )

    worksheet_path = destination / REVIEW_WORKSHEET_NAME
    _write_csv(worksheet_path, OWNER_REVIEW_FIELDS, rows)
    signoff = {
        "schema_version": OWNER_REVIEW_VERSION,
        "dataset": dataset.manifest.get("dataset"),
        "dataset_version": dataset.manifest.get("dataset_version"),
        "dataset_manifest_sha256": manifest_sha,
        "packet_id": packet_id,
        "review_worksheet": REVIEW_WORKSHEET_NAME,
        "review_worksheet_sha256": "",
        "worksheet_items": len(rows),
        "reviewer_id": "",
        "reviewer_name": "",
        "signature": "",
        "reviewed_at": "",
        "owner_attested": False,
        "attestation": "本人已逐题复核本数据包的答案、分值和来源定位，并对其中的变更与保留项负责。",
        "notes": "",
    }
    signoff_path = destination / SIGNOFF_NAME
    signoff_path.write_text(json.dumps(signoff, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (destination / "README.md").write_text(_review_instructions(dataset, packet_id), encoding="utf-8")
    release_notes = _release_review_notes_path(dataset)
    if release_notes.is_file():
        shutil.copy2(release_notes, destination / "REVIEW_NOTES.md")
    else:
        (destination / "REVIEW_NOTES.md").write_text(_review_notes(dataset), encoding="utf-8")

    packet_manifest = {
        "packet_id": packet_id,
        "packet_version": OWNER_REVIEW_VERSION,
        "created_at": created_at,
        "dataset_version": dataset.manifest.get("dataset_version"),
        "dataset_manifest_sha256": manifest_sha,
        "review_scope": "owner_single_signoff_not_independent_double_review",
        "counts": {
            "answer_items": len(rows),
            "point_items": len(rows),
            "questions": len(rows),
            "points": sum(int(question["points"]) for question in dataset.questions),
        },
        "public_release": False,
        "rights": "internal_review_only_not_for_redistribution",
        "files": {
            name: {"sha256": _file_sha256(destination / name), "bytes": (destination / name).stat().st_size}
            for name in (REVIEW_WORKSHEET_NAME, SIGNOFF_NAME, "README.md", "REVIEW_NOTES.md")
        },
    }
    (destination / "packet_manifest.json").write_text(
        json.dumps(packet_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return packet_manifest


def _parse_review_timestamp(value: Any, errors: List[str]) -> Optional[dt.datetime]:
    text = str(value or "").strip()
    if not text:
        errors.append("reviewed_at is empty")
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append("reviewed_at is not ISO-8601")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append("reviewed_at must include a timezone offset")
        return None
    return parsed


def _normalise_answer(value: str) -> Any:
    text = (value or "").strip()
    if not text:
        return None
    if text.startswith("[") or text.startswith("{") or text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _answers_equal(left: Any, right: Any) -> bool:
    return _json_text(left) == _json_text(right)


def import_owner_signoff(
    data_dir: Union[Path, str],
    *,
    signoff: Union[Path, str],
    review_csv: Union[Path, str],
) -> Dict[str, Any]:
    """Validate one owner signoff and emit proposals without modifying data."""

    dataset = load_dataset(data_dir)
    validate_dataset(dataset)
    signoff_path = Path(signoff).resolve()
    worksheet_path = Path(review_csv).resolve()
    errors: List[str] = []
    try:
        payload = json.loads(signoff_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnerReviewValidationError("signoff JSON cannot be read: %s" % exc) from exc
    if not isinstance(payload, dict):
        raise OwnerReviewValidationError("signoff JSON must be an object")

    manifest_sha = _file_sha256(dataset.data_dir / "manifest.json")
    if payload.get("dataset_version") != dataset.manifest.get("dataset_version"):
        errors.append("dataset_version in signoff does not match the data package")
    if payload.get("dataset_manifest_sha256") != manifest_sha:
        errors.append("dataset_manifest_sha256 does not match the frozen package")
    if payload.get("review_worksheet") not in ("", None, REVIEW_WORKSHEET_NAME):
        errors.append("review_worksheet must be %s" % REVIEW_WORKSHEET_NAME)
    if int(payload.get("worksheet_items") or -1) != len(dataset.questions):
        errors.append("worksheet_items does not match the data package")
    if payload.get("owner_attested") is not True:
        errors.append("owner_attested must be true")

    reviewer_id = str(payload.get("reviewer_id", "")).strip()
    reviewer_name = str(payload.get("reviewer_name", "")).strip()
    signature = str(payload.get("signature", "")).strip()
    if not reviewer_id or any(marker in reviewer_id.lower() for marker in INVALID_IDENTITY_MARKERS):
        errors.append("reviewer_id is empty or provisional")
    if not reviewer_name or any(marker in reviewer_name.lower() for marker in INVALID_IDENTITY_MARKERS):
        errors.append("reviewer_name is empty or provisional")
    if not signature or signature != reviewer_name:
        errors.append("signature must exactly equal reviewer_name")
    _parse_review_timestamp(payload.get("reviewed_at"), errors)

    supplied_worksheet_sha = str(payload.get("review_worksheet_sha256", "")).strip()
    actual_worksheet_sha = _file_sha256(worksheet_path)
    if supplied_worksheet_sha and supplied_worksheet_sha != actual_worksheet_sha:
        errors.append("review_worksheet_sha256 does not match the submitted CSV")

    try:
        fieldnames, rows = _read_csv(worksheet_path)
    except (OSError, OwnerReviewValidationError) as exc:
        raise OwnerReviewValidationError("owner review worksheet cannot be read: %s" % exc) from exc
    missing_columns = sorted(set(OWNER_REVIEW_FIELDS) - set(fieldnames))
    if missing_columns:
        errors.append("owner review worksheet is missing columns: %s" % ", ".join(missing_columns))

    expected = {question["id"]: question for question in dataset.questions}
    seen: set = set()
    decisions: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    for index, row in enumerate(rows, 2):
        item_id = str(row.get("item_id", "")).strip()
        if not item_id:
            errors.append("row %d: item_id is empty" % index)
            continue
        if item_id in seen:
            errors.append("row %d: duplicate item_id %s" % (index, item_id))
            continue
        seen.add(item_id)
        question = expected.get(item_id)
        if question is None:
            errors.append("row %d: unknown item_id %s" % (index, item_id))
            continue
        decision = str(row.get("decision", "")).strip().lower()
        if decision not in ALLOWED_DECISIONS:
            errors.append("row %d: decision must be agree/correct/dispute" % index)
            continue
        notes = str(row.get("notes", "")).strip()
        canonical_answer = dataset.answer_keys[item_id].get("canonical")
        canonical_points = int(question["points"])
        reviewed_answer = _normalise_answer(str(row.get("reviewed_answer", "")))
        raw_points = str(row.get("reviewed_points", "")).strip()
        reviewed_points: Optional[int] = None
        if raw_points:
            try:
                numeric = float(raw_points)
                if not numeric.is_integer() or numeric < 0 or numeric > 50:
                    raise ValueError
                reviewed_points = int(numeric)
            except ValueError:
                errors.append("row %d: reviewed_points must be an integer from 0 to 50" % index)
                continue

        if decision == "agree":
            if reviewed_answer is not None and not _answers_equal(reviewed_answer, canonical_answer):
                errors.append("row %d: changed answer requires decision=correct" % index)
            if reviewed_points is not None and reviewed_points != canonical_points:
                errors.append("row %d: changed points require decision=correct" % index)
            final_answer = canonical_answer
            final_points = canonical_points
        elif decision == "correct":
            if not notes:
                errors.append("row %d: correct decision requires notes" % index)
            if reviewed_answer is None and reviewed_points is None:
                errors.append("row %d: correct decision requires reviewed_answer and/or reviewed_points" % index)
            if reviewed_answer is None:
                reviewed_answer = canonical_answer
            if reviewed_points is None:
                reviewed_points = canonical_points
            if _answers_equal(reviewed_answer, canonical_answer) and reviewed_points == canonical_points:
                errors.append("row %d: correct decision does not change answer or points" % index)
            final_answer = reviewed_answer
            final_points = reviewed_points
        else:
            if not notes:
                errors.append("row %d: dispute decision requires notes" % index)
            final_answer = None
            final_points = None

        decisions.append(
            {
                "item_id": item_id,
                "decision": decision,
                "canonical_answer": canonical_answer,
                "reviewed_answer": reviewed_answer,
                "canonical_points": canonical_points,
                "reviewed_points": reviewed_points,
                "final_answer": final_answer,
                "final_points": final_points,
                "notes": notes,
            }
        )
        if decision == "correct" and final_answer is not None and final_points is not None:
            proposals.append(
                {
                    "item_type": "answer_and_points",
                    "item_id": item_id,
                    "before_answer": canonical_answer,
                    "after_answer": final_answer,
                    "before_points": canonical_points,
                    "after_points": final_points,
                    "reason": notes,
                }
            )

    missing = sorted(set(expected) - seen)
    if missing:
        errors.append("owner review worksheet is missing %d items: %s" % (len(missing), ", ".join(missing[:8])))
    disputes = [row for row in decisions if row["decision"] == "dispute"]
    if errors:
        status = "invalid"
    elif disputes:
        status = "owner_disputed"
    elif proposals:
        status = "complete_with_proposals"
    else:
        status = "complete"
    owner_signoff = "complete" if status in ("complete", "complete_with_proposals") else "pending"

    return {
        "schema_version": OWNER_REVIEW_VERSION,
        "report_type": "owner_signoff_import",
        "dataset": dataset.manifest.get("dataset"),
        "dataset_version": dataset.manifest.get("dataset_version"),
        "dataset_manifest_sha256": manifest_sha,
        "dataset_mutated": False,
        "owner_signoff": owner_signoff,
        "human_independent_review": "pending",
        "status": status,
        "review_scope": "owner_single_signoff_not_independent_double_review",
        "reviewer": {
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "signature": signature,
            "reviewed_at": payload.get("reviewed_at"),
        },
        "attestation": payload.get("attestation"),
        "input_sha256": {
            "signoff": _file_sha256(signoff_path),
            REVIEW_WORKSHEET_NAME: actual_worksheet_sha,
            "manifest": manifest_sha,
        },
        "counts": {
            "expected_items": len(expected),
            "reviewed_items": len(decisions),
            "agree": sum(1 for row in decisions if row["decision"] == "agree"),
            "correct": sum(1 for row in decisions if row["decision"] == "correct"),
            "dispute": len(disputes),
            "proposed_changes": len(proposals),
            "errors": len(errors),
        },
        "errors": errors,
        "disputes": disputes,
        "proposals": proposals,
        "items": decisions,
        "next_step": (
            "Archive this signed report. If proposals exist, create a new immutable dataset version and re-export the review packet."
            if status == "complete_with_proposals"
            else "Archive this signed report. Independent two-person review remains pending."
            if status == "complete"
            else "Resolve disputes or validation errors, then re-import. Nothing was applied automatically."
        ),
    }


def write_owner_report(report: Mapping[str, Any], output: Union[Path, str]) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
