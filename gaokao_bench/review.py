"""Export and import the independent-review packet for Gaokao-Bench.

The import path is deliberately read-only with respect to the dataset.  It
validates two independent reviewer files and any required arbitration, then
emits an auditable report and proposed revisions.  A successful import is
evidence for a later dataset revision, not permission to rewrite canonical
answers in place.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .scoring import Dataset, load_dataset, validate_dataset

REVIEW_PACKET_VERSION = "1.1"
REVIEWER_SLOTS = (1, 2)
REVIEW_METADATA_FIELDS = (
    "reviewer_id",
    "reviewer_name",
    "signature",
    "reviewed_at",
)
ANSWER_REVIEW_FIELDS = (
    "item_id",
    "paper_id",
    "display_number",
    "question_type",
    "stem",
    "options",
    "canonical_answer",
    "current_answer",
    "evidence_status",
    "external_source_id",
    "external_page_url",
    "external_image_url",
    "external_locator",
    "external_excerpt",
    "decision",
    "reviewed_answer",
    "notes",
) + REVIEW_METADATA_FIELDS
POINT_REVIEW_FIELDS = (
    "item_id",
    "paper_id",
    "display_number",
    "kind",
    "item_label",
    "current_points",
    "reviewed_points_baseline",
    "evidence_status",
    "locator",
    "match_method",
    "source_ids",
    "decision",
    "reviewed_points",
    "notes",
) + REVIEW_METADATA_FIELDS
ALLOWED_DECISIONS = {"agree", "correct", "dispute"}
PROVISIONAL_REVIEWER_MARKERS = (
    "pending",
    "codex",
    "agent",
    "provisional",
    "automatic",
    "automated",
)


class ReviewValidationError(ValueError):
    """Raised when reviewer input is incomplete or internally inconsistent."""


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReviewValidationError("invalid JSONL at %s:%d: %s" % (path, line_number, exc)) from exc
            if not isinstance(row, dict):
                raise ReviewValidationError("JSONL row must be an object at %s:%d" % (path, line_number))
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


def _one_line(value: Any, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _source_page(source: Mapping[str, Any], locator: Any) -> Optional[Mapping[str, Any]]:
    match = re.search(r"page-(\d+)", str(locator or ""))
    if not match:
        page = None
    else:
        page = next(
            (item for item in source.get("pages", []) if item.get("page_index") == int(match.group(1))),
            None,
        )
    if page is None and len(source.get("pages", [])) == 1:
        page = source["pages"][0]
    return page


def _source_page_url(source: Mapping[str, Any], locator: Any) -> str:
    page = _source_page(source, locator)
    if page is not None:
        return str(page.get("page_url", ""))
    return str(source.get("url", ""))


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
            raise ReviewValidationError("review CSV has no header: %s" % path)
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames), rows


def _reviewer_readme(slot: int, packet_id: str) -> str:
    return """# Gaokao-Bench 独立复核包（审校者 {slot}）

> Packet: `{packet_id}`  
> 机密等级：内部审校材料。不得公开转发或再分发。  
> 当前 canonical 答案尚未完成签字确认，本文件不构成官方答案声明。

## 你要做什么

1. 独立核验 `answers_review.csv` 中 74 道客观题的题干、选项、canonical answer 和证据定位；
2. 独立核验 `points_review.csv` 中 74 道选择/填空题分值；
3. 在每一行填写相同且真实的审校者元数据；
4. 对 `decision` 只填 `agree`、`correct` 或 `dispute`；
5. `reviewed_answer` / `reviewed_points` 仅在 `correct` 时必填；
6. `notes` 在 `correct` 或 `dispute` 时必填；
7. 不要与其他审校者讨论或查看对方文件；冲突交由第三方仲裁。

`reviewed_at` 使用带时区的 ISO-8601，例如
`2026-11-10T14:30:00+08:00`。`signature` 必须与 `reviewer_name` 完全一致。

多空答案写成 JSON 数组，例如 `["1", "15"]`；单选题可写 `C`。
来源通常是分页答案图片，网页正文可能搜不到题号。请优先打开
`external_image_url` 指向的已校验答案图，再用 `external_locator` 核对；
`external_page_url` 仅作为文章入口。
不要改动 `item_id`、`canonical_answer`、`current_points` 等基线列。

## 提交

交回本目录下的 `answers_review.csv` 与 `points_review.csv`。导入器会检查
两份文件是否同属一名审校者、是否覆盖全部条目、是否完整签字，并拒绝
`pending`/自动代理占位值。任何冲突都必须由第三名仲裁者另行签署。
""".format(slot=slot, packet_id=packet_id)


def _assignment_manifest(packet_id: str, slot: int, created_at: str, dataset: Dataset) -> Dict[str, Any]:
    return {
        "packet_id": packet_id,
        "packet_version": REVIEW_PACKET_VERSION,
        "reviewer_slot": slot,
        "created_at": created_at,
        "dataset": dataset.manifest.get("dataset"),
        "dataset_version": dataset.manifest.get("dataset_version"),
        "dataset_manifest_sha256": _file_sha256(dataset.data_dir / "manifest.json"),
        "instructions": "INDEPENDENT_REVIEW_ONLY_DO_NOT_CONSULT_OTHER_REVIEWER",
        "review_items": {
            "answers": len(dataset.questions),
            "points": len(dataset.questions),
        },
        "public_release": False,
        "rights": "internal_review_only_not_for_redistribution",
    }


def export_review_packet(
    data_dir: Path | str,
    out_dir: Path | str,
    *,
    created_at: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Write two blank, independent review assignments and return a manifest."""

    dataset = load_dataset(data_dir)
    validate_dataset(dataset)
    destination = Path(out_dir).resolve()
    if destination.exists() and any(destination.iterdir()):
        if not force:
            raise ReviewValidationError("output directory is not empty; pass force=True to replace it: %s" % destination)
        if (
            destination == dataset.data_dir
            or dataset.data_dir in destination.parents
            or destination in dataset.data_dir.parents
        ):
            raise ReviewValidationError("refusing to replace the data package or one of its parent directories")
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    created_at = created_at or dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    packet_id = "GB-REVIEW-%s-%s" % (
        dataset.manifest["dataset_version"],
        _file_sha256(dataset.data_dir / "manifest.json")[:12],
    )

    questions = {question["id"]: question for question in dataset.questions}
    answer_audit = {row["question_id"]: row for row in _read_jsonl(dataset.data_dir / "answer_audit.jsonl")}
    point_audit = _read_jsonl(dataset.data_dir / "point_audit.jsonl")
    answer_sources = _read_jsonl(dataset.data_dir / "answer_sources.jsonl")
    answer_sources_by_id = {row.get("id"): row for row in answer_sources}
    answer_rows: List[Dict[str, Any]] = []
    for question in dataset.questions:
        qid = question["id"]
        audit = answer_audit[qid]
        key = dataset.answer_keys[qid]
        source = answer_sources_by_id.get(audit.get("external_source_id"), {})
        source_page = _source_page(source, audit.get("external_locator"))
        answer_rows.append(
            {
                "item_id": qid,
                "paper_id": question["paper_id"],
                "display_number": question.get("display_number", ""),
                "question_type": question["type"],
                "stem": _one_line(question.get("stem") or question.get("content"), 700),
                "options": _json_text(question.get("options", [])),
                "canonical_answer": _json_text(key.get("canonical")),
                "current_answer": _json_text(audit.get("current_canonical")),
                "evidence_status": audit.get("status", ""),
                "external_source_id": audit.get("external_source_id", ""),
                "external_page_url": _source_page_url(source, audit.get("external_locator")),
                "external_image_url": str(source_page.get("image_url", "")) if source_page else "",
                "external_locator": audit.get("external_locator", ""),
                "external_excerpt": _one_line(audit.get("external_excerpt"), 500),
                "decision": "",
                "reviewed_answer": "",
                "notes": "",
                "reviewer_id": "",
                "reviewer_name": "",
                "signature": "",
                "reviewed_at": "",
            }
        )

    point_rows: List[Dict[str, Any]] = []
    for audit in sorted(point_audit, key=lambda row: (row["paper_id"], int(row["number"]))):
        item_id = audit["item_id"]
        source_item = questions[audit["question_id"]]
        label = source_item.get("stem") or source_item.get("content")
        display_number = source_item.get("display_number", "")
        point_rows.append(
            {
                "item_id": item_id,
                "paper_id": audit["paper_id"],
                "display_number": display_number,
                "kind": audit["kind"],
                "item_label": _one_line(label, 500),
                "current_points": audit.get("current_points", ""),
                "reviewed_points_baseline": audit.get("reviewed_points", ""),
                "evidence_status": audit.get("status", ""),
                "locator": audit.get("locator", ""),
                "match_method": audit.get("match_method", ""),
                "source_ids": _json_text(audit.get("source_ids", [])),
                "decision": "",
                "reviewed_points": "",
                "notes": "",
                "reviewer_id": "",
                "reviewer_name": "",
                "signature": "",
                "reviewed_at": "",
            }
        )

    source_rows = [
        {
            "source_id": source.get("id"),
            "name": source.get("name"),
            "source_type": source.get("source_type"),
            "official": source.get("official"),
            "url": source.get("url") or source.get("source_url"),
            "rights_status": source.get("rights_status"),
        }
        for source in answer_sources
    ]

    assignment_files: Dict[str, Dict[str, Any]] = {}
    for slot in REVIEWER_SLOTS:
        reviewer_dir = destination / ("reviewer-%d" % slot)
        reviewer_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(reviewer_dir / "answers_review.csv", ANSWER_REVIEW_FIELDS, answer_rows)
        _write_csv(reviewer_dir / "points_review.csv", POINT_REVIEW_FIELDS, point_rows)
        _write_csv(
            reviewer_dir / "evidence_sources.csv",
            ("source_id", "name", "source_type", "official", "url", "rights_status"),
            source_rows,
        )
        (reviewer_dir / "README.md").write_text(
            _reviewer_readme(slot, packet_id), encoding="utf-8"
        )
        assignment = _assignment_manifest(packet_id, slot, created_at, dataset)
        (reviewer_dir / "assignment.json").write_text(
            json.dumps(assignment, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        assignment_files["reviewer-%d" % slot] = {
            name: {
                "sha256": _file_sha256(reviewer_dir / name),
                "bytes": (reviewer_dir / name).stat().st_size,
            }
            for name in (
                "answers_review.csv",
                "points_review.csv",
                "evidence_sources.csv",
                "README.md",
                "assignment.json",
            )
        }

    arbitration_template = {
        "packet_id": packet_id,
        "instructions": "Only fill conflicts reported by import_review.py. The arbiter must be a third person.",
        "arbitrations": [],
    }
    (destination / "arbitration_template.json").write_text(
        json.dumps(arbitration_template, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet_readme = """# Gaokao-Bench 独立双人复核包

> Packet: `{packet_id}`  
> Dataset: `{dataset_version}`  
> Manifest SHA-256: `{manifest_sha}`  
> 范围：{answer_items} 道题的答案复核 + {point_items} 条分值复核

## 分工

- `reviewer-1/`：审校者甲，独立填写 `answers_review.csv` 与 `points_review.csv`；
- `reviewer-2/`：审校者乙，独立填写同一套文件；
- 两人不得查看对方文件、讨论答案或共用审校身份；
- 冲突由第三名仲裁者按 `arbitration_template.json` 单独签字。

## 填写要求

1. 每一行 `decision` 只填 `agree`、`correct` 或 `dispute`；
2. `correct` 填写 `reviewed_answer` 或 `reviewed_points`，并写明 `notes`；
3. `dispute` 必须写明争议点；
4. `reviewer_id`、`reviewer_name`、`signature`、`reviewed_at` 必须真实且一致；
5. 多空答案写成 JSON 数组，例如 `["1", "15"]`；单选题可写 `C`；
6. 不要修改 `item_id`、`canonical_answer`、`current_points` 等基线列。

来源通常是分页答案图片，网页正文可能搜不到题号。请优先打开
`answers_review.csv` 中 `external_image_url` 指向的答案图，再用
`external_locator` 核对；`external_page_url` 只是文章入口。

## 导入

两人完成后由数据负责人运行：

```bash
python3 scripts/import_review.py \\
  --data-dir data/{dataset_version} \\
  --answers-1 reviewer-1/answers_review.csv \\
  --answers-2 reviewer-2/answers_review.csv \\
  --points-1 reviewer-1/points_review.csv \\
  --points-2 reviewer-2/points_review.csv \\
  --output reports/{dataset_version}-independent-review-import.json
```

若导入报告给出冲突，填写 `arbitration_template.json` 后加 `--arbitration` 重跑。导入只生成报告和变更建议，不修改数据。
""".format(
        packet_id=packet_id,
        dataset_version=dataset.manifest["dataset_version"],
        manifest_sha=_file_sha256(dataset.data_dir / "manifest.json"),
        answer_items=len(answer_rows),
        point_items=len(point_rows),
    )
    (destination / "README.md").write_text(packet_readme, encoding="utf-8")
    manifest = {
        "packet_id": packet_id,
        "packet_version": REVIEW_PACKET_VERSION,
        "created_at": created_at,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_manifest_sha256": _file_sha256(dataset.data_dir / "manifest.json"),
        "public_release": False,
        "rights": "internal_review_only_not_for_redistribution",
        "counts": {"answer_items": len(answer_rows), "point_items": len(point_rows), "reviewers": 2},
        "assignments": assignment_files,
        "arbitration_template_sha256": _file_sha256(destination / "arbitration_template.json"),
        "README.md": {
            "sha256": _file_sha256(destination / "README.md"),
            "bytes": (destination / "README.md").stat().st_size,
        },
    }
    (destination / "packet_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _parse_iso_timestamp(value: str, field: str, errors: List[str], location: str) -> Optional[dt.datetime]:
    text = (value or "").strip()
    if not text:
        errors.append("%s: %s is empty" % (location, field))
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        errors.append("%s: %s is not ISO-8601" % (location, field))
        return None
    if parsed.tzinfo is None:
        errors.append("%s: %s must include a timezone" % (location, field))
        return None
    return parsed


def _parse_review_value(text: str) -> Any:
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return stripped


def _value_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _validate_reviewer_metadata(
    rows: Sequence[Mapping[str, str]],
    *,
    expected_items: set,
    value_field: str,
    kind: str,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    values: Dict[str, Any] = {}
    decisions: List[Dict[str, Any]] = []
    if not rows:
        return {}, [], ["%s review is empty" % kind]
    first = rows[0]
    reviewer_id = (first.get("reviewer_id") or "").strip()
    reviewer_name = (first.get("reviewer_name") or "").strip()
    signature = (first.get("signature") or "").strip()
    reviewed_at = (first.get("reviewed_at") or "").strip()
    if not reviewer_id:
        errors.append("%s review: reviewer_id is empty" % kind)
    lowered = reviewer_id.lower()
    if any(marker in lowered for marker in PROVISIONAL_REVIEWER_MARKERS):
        errors.append("%s review: reviewer_id looks provisional or automated: %s" % (kind, reviewer_id))
    if not reviewer_name:
        errors.append("%s review: reviewer_name is empty" % kind)
    if signature != reviewer_name:
        errors.append("%s review: signature must equal reviewer_name" % kind)
    _parse_iso_timestamp(reviewed_at, "reviewed_at", errors, "%s review" % kind)
    seen: set = set()
    for index, row in enumerate(rows, 2):
        location = "%s:%d" % (kind, index)
        item_id = (row.get("item_id") or "").strip()
        if item_id not in expected_items:
            errors.append("%s: unexpected item_id %r" % (location, item_id))
            continue
        if item_id in seen:
            errors.append("%s: duplicate item_id %s" % (location, item_id))
            continue
        seen.add(item_id)
        for field, expected in (
            ("reviewer_id", reviewer_id),
            ("reviewer_name", reviewer_name),
            ("signature", signature),
        ):
            if (row.get(field) or "").strip() != expected:
                errors.append("%s: inconsistent %s across file" % (location, field))
        _parse_iso_timestamp(row.get("reviewed_at", ""), "reviewed_at", errors, location)
        decision = (row.get("decision") or "").strip().lower()
        if decision not in ALLOWED_DECISIONS:
            errors.append("%s: decision must be agree/correct/dispute" % location)
            continue
        raw_value = row.get(value_field, "")
        parsed_value = _parse_review_value(raw_value)
        if decision == "correct" and parsed_value is None:
            errors.append("%s: correct requires %s" % (location, value_field))
        if decision == "dispute" and not (row.get("notes") or "").strip():
            errors.append("%s: dispute requires notes" % location)
        if decision == "correct" and not (row.get("notes") or "").strip():
            errors.append("%s: correct requires notes" % location)
        if decision == "agree" and parsed_value is not None:
            errors.append("%s: agree must leave %s empty" % (location, value_field))
        if kind == "points" and decision == "correct":
            try:
                number = float(parsed_value)
                if not number.is_integer() or number < 0 or number > 50:
                    raise ValueError
                parsed_value = int(number)
            except (TypeError, ValueError):
                errors.append("%s: reviewed_points must be an integer from 0 to 50" % location)
        values[item_id] = parsed_value
        decisions.append(
            {
                "item_id": item_id,
                "decision": decision,
                "value": parsed_value,
                "notes": (row.get("notes") or "").strip(),
                "reviewed_at": (row.get("reviewed_at") or "").strip(),
            }
        )
    missing = sorted(expected_items - seen)
    if missing:
        errors.append("%s review is missing %d items: %s" % (kind, len(missing), ", ".join(missing[:8])))
    metadata = {
        "reviewer_id": reviewer_id,
        "reviewer_name": reviewer_name,
        "signature": signature,
        "reviewed_at": reviewed_at,
    }
    return metadata, decisions, errors


def _load_arbitrations(path: Optional[Path], expected_blockers: set, errors: List[str]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append("arbitration file cannot be read: %s" % exc)
        return {}
    rows = payload.get("arbitrations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        errors.append("arbitration file must contain an arbitrations list")
        return {}
    results: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for index, row in enumerate(rows, 1):
        location = "arbitration:%d" % index
        if not isinstance(row, dict):
            errors.append("%s must be an object" % location)
            continue
        item_type = str(row.get("item_type", "")).strip().lower()
        item_id = str(row.get("item_id", "")).strip()
        key = (item_type, item_id)
        if item_type not in ("answer", "points") or not item_id:
            errors.append("%s has invalid item_type/item_id" % location)
            continue
        if key in results:
            errors.append("%s duplicates %s/%s" % (location, item_type, item_id))
            continue
        if key not in expected_blockers:
            errors.append("%s is not a reported conflict: %s/%s" % (location, item_type, item_id))
        arbiter_id = str(row.get("arbiter_id", "")).strip()
        arbiter_name = str(row.get("arbiter_name", "")).strip()
        signature = str(row.get("signature", "")).strip()
        if not arbiter_id or any(marker in arbiter_id.lower() for marker in PROVISIONAL_REVIEWER_MARKERS):
            errors.append("%s: arbiter_id is empty or provisional" % location)
        if not arbiter_name or signature != arbiter_name:
            errors.append("%s: arbiter signature must equal arbiter_name" % location)
        _parse_iso_timestamp(str(row.get("decided_at", "")), "decided_at", errors, location)
        if not str(row.get("rationale", "")).strip():
            errors.append("%s: rationale is required" % location)
        decision = str(row.get("decision", "")).strip().lower()
        if decision not in ("accept", "correct"):
            errors.append("%s: decision must be accept/correct" % location)
            continue
        final_value = row.get("final_value")
        if item_type == "points":
            try:
                numeric = float(final_value)
                if not numeric.is_integer() or numeric < 0 or numeric > 50:
                    raise ValueError
                final_value = int(numeric)
            except (TypeError, ValueError):
                errors.append("%s: final_value must be an integer from 0 to 50" % location)
                continue
        elif decision == "correct" and final_value in (None, ""):
            errors.append("%s: corrected answer requires final_value" % location)
            continue
        results[key] = {
            "arbiter_id": arbiter_id,
            "arbiter_name": arbiter_name,
            "signature": signature,
            "decided_at": row.get("decided_at"),
            "decision": decision,
            "final_value": final_value,
            "rationale": str(row.get("rationale", "")).strip(),
        }
    return results


def import_review_packet(
    data_dir: Path | str,
    *,
    answers_1: Path | str,
    answers_2: Path | str,
    points_1: Path | str,
    points_2: Path | str,
    arbitration: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Validate two signed reviewer submissions and produce a proposal report."""

    dataset = load_dataset(data_dir)
    validate_dataset(dataset)
    expected_answers = {question["id"] for question in dataset.questions}
    expected_points = {row["item_id"] for row in _read_jsonl(dataset.data_dir / "point_audit.jsonl")}
    files = [
        ("answers", 1, Path(answers_1), expected_answers, "reviewed_answer"),
        ("answers", 2, Path(answers_2), expected_answers, "reviewed_answer"),
        ("points", 1, Path(points_1), expected_points, "reviewed_points"),
        ("points", 2, Path(points_2), expected_points, "reviewed_points"),
    ]
    errors: List[str] = []
    parsed: Dict[Tuple[str, int], Tuple[Dict[str, Any], List[Dict[str, Any]], Path]] = {}
    file_hashes: Dict[str, str] = {}
    for kind, slot, path, expected, value_field in files:
        try:
            fieldnames, rows = _read_csv(path)
        except (OSError, ReviewValidationError) as exc:
            errors.append("%s reviewer-%d file cannot be read: %s" % (kind, slot, exc))
            continue
        required = set(ANSWER_REVIEW_FIELDS if kind == "answers" else POINT_REVIEW_FIELDS)
        missing_columns = sorted(required - set(fieldnames))
        if missing_columns:
            errors.append("%s reviewer-%d is missing columns: %s" % (kind, slot, ", ".join(missing_columns)))
        metadata, decisions, item_errors = _validate_reviewer_metadata(
            rows, expected_items=expected, value_field=value_field, kind="%s.reviewer-%d" % (kind, slot)
        )
        errors.extend(item_errors)
        parsed[(kind, slot)] = (metadata, decisions, path)
        file_hashes["%s.reviewer-%d" % (kind, slot)] = _file_sha256(path)

    reviewer_metadata: Dict[int, Dict[str, Any]] = {}
    for slot in REVIEWER_SLOTS:
        answer_meta = parsed.get(("answers", slot), ({}, [], None))[0]
        point_meta = parsed.get(("points", slot), ({}, [], None))[0]
        if answer_meta.get("reviewer_id") != point_meta.get("reviewer_id"):
            errors.append("reviewer-%d answer/point files use different reviewer_id" % slot)
        if answer_meta.get("reviewer_name") != point_meta.get("reviewer_name"):
            errors.append("reviewer-%d answer/point files use different reviewer_name" % slot)
        if answer_meta.get("signature") != point_meta.get("signature"):
            errors.append("reviewer-%d answer/point files use different signature" % slot)
        reviewer_metadata[slot] = answer_meta
    if reviewer_metadata[1].get("reviewer_id") == reviewer_metadata[2].get("reviewer_id"):
        errors.append("reviewer-1 and reviewer-2 must be different people")
    if reviewer_metadata[1].get("reviewer_name") == reviewer_metadata[2].get("reviewer_name"):
        errors.append("reviewer-1 and reviewer-2 must have different names")

    answer_decisions = {
        slot: {row["item_id"]: row for row in parsed.get(("answers", slot), ({}, [], None))[1]}
        for slot in REVIEWER_SLOTS
    }
    point_decisions = {
        slot: {row["item_id"]: row for row in parsed.get(("points", slot), ({}, [], None))[1]}
        for slot in REVIEWER_SLOTS
    }
    canonical_answers = {qid: dataset.answer_keys[qid].get("canonical") for qid in expected_answers}
    baseline_points = {
        row["item_id"]: row.get("reviewed_points")
        for row in _read_jsonl(dataset.data_dir / "point_audit.jsonl")
    }

    items: List[Dict[str, Any]] = []
    blockers: List[Dict[str, Any]] = []
    for item_type, expected, canonical_map, decision_map in (
        ("answer", expected_answers, canonical_answers, answer_decisions),
        ("points", expected_points, baseline_points, point_decisions),
    ):
        for item_id in sorted(expected):
            canonical = canonical_map[item_id]
            first = decision_map[1].get(item_id)
            second = decision_map[2].get(item_id)
            if first is None or second is None:
                blockers.append({"item_type": item_type, "item_id": item_id, "reason": "missing_review"})
                continue
            resolved_first = canonical if first["decision"] == "agree" else first["value"]
            resolved_second = canonical if second["decision"] == "agree" else second["value"]
            conflict = (
                first["decision"] == "dispute"
                or second["decision"] == "dispute"
                or _value_key(resolved_first) != _value_key(resolved_second)
            )
            item = {
                "item_type": item_type,
                "item_id": item_id,
                "canonical_value": canonical,
                "reviewer_values": [resolved_first, resolved_second],
                "decisions": [first["decision"], second["decision"]],
                "conflict": conflict,
                "arbitration_required": conflict,
                "final_value": None,
                "action": None,
                "arbitrated": False,
            }
            if conflict:
                blocker = {"item_type": item_type, "item_id": item_id, "reason": "reviewer_conflict"}
                blockers.append(blocker)
                item["blocker_reason"] = "reviewer_conflict"
            else:
                agreed = resolved_first
                if _value_key(agreed) == _value_key(canonical):
                    item["final_value"] = canonical
                    item["action"] = "no_change"
                else:
                    item["final_value"] = agreed
                    item["action"] = "propose_change"
            items.append(item)

    blocker_keys = {(row["item_type"], row["item_id"]) for row in blockers if row["reason"] == "reviewer_conflict"}
    arbitrations = _load_arbitrations(Path(arbitration) if arbitration else None, blocker_keys, errors)
    item_by_key = {(row["item_type"], row["item_id"]): row for row in items}
    for key, arbitration in arbitrations.items():
        item = item_by_key.get(key)
        if item is None:
            continue
        if arbitration["arbiter_id"] in (
            reviewer_metadata[1].get("reviewer_id"),
            reviewer_metadata[2].get("reviewer_id"),
        ):
            errors.append("arbiter for %s/%s must be a third person" % key)
            continue
        canonical = item["canonical_value"]
        if arbitration["decision"] == "accept":
            final_value = canonical
        else:
            final_value = arbitration["final_value"]
        item["final_value"] = final_value
        item["action"] = "no_change" if _value_key(final_value) == _value_key(canonical) else "propose_change"
        item["arbitrated"] = True
        item["arbitration"] = arbitration
        blockers = [row for row in blockers if (row["item_type"], row["item_id"]) != key]

    unresolved = [
        row
        for row in items
        if row["final_value"] is None or row["action"] is None or row.get("arbitration_required") and not row.get("arbitrated")
    ]
    if unresolved:
        blockers.extend(
            {
                "item_type": row["item_type"],
                "item_id": row["item_id"],
                "reason": "arbitration_missing",
            }
            for row in unresolved
            if not any(b["item_type"] == row["item_type"] and b["item_id"] == row["item_id"] for b in blockers)
        )

    proposals = [
        {
            "item_type": item["item_type"],
            "item_id": item["item_id"],
            "before": item["canonical_value"],
            "after": item["final_value"],
            "reason": "two independent reviewers and arbitration" if item["arbitrated"] else "two independent reviewers agree",
        }
        for item in items
        if item["action"] == "propose_change"
    ]
    complete = not errors and not blockers and all(item["final_value"] is not None for item in items)
    status = "complete" if complete else ("invalid" if errors else "pending_arbitration")
    return {
        "report_version": REVIEW_PACKET_VERSION,
        "dataset_version": dataset.manifest["dataset_version"],
        "dataset_manifest_sha256": _file_sha256(dataset.data_dir / "manifest.json"),
        "dataset_mutated": False,
        "human_independent_review": "complete" if complete else "pending",
        "status": status,
        "reviewers": {str(slot): reviewer_metadata[slot] for slot in REVIEWER_SLOTS},
        "input_sha256": file_hashes,
        "counts": {
            "answer_items": len(expected_answers),
            "point_items": len(expected_points),
            "reviewed_items": len(items),
            "arbitrated_items": sum(1 for item in items if item["arbitrated"]),
            "proposed_changes": len(proposals),
            "blocking_items": len(blockers),
            "errors": len(errors),
        },
        "errors": errors,
        "blocking_items": sorted(blockers, key=lambda row: (row["item_type"], row["item_id"], row["reason"])),
        "items": items,
        "proposals": proposals,
        "next_step": (
            "Archive this report, then produce a new immutable dataset version with the proposed changes and fresh manifest hashes."
            if complete and proposals
            else "Nothing is applied automatically." if complete
            else "Resolve errors and conflicts, then rerun import."
        ),
    }


def write_review_report(report: Mapping[str, Any], output: Path | str) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
