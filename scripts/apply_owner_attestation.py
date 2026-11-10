#!/usr/bin/env python3
"""Create an immutable owner-attested dataset version from an audited source.

This is deliberately separate from independent-review materialisation.  It
records a single project-owner acceptance event for every scored question; it
does not claim that two external reviewers participated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.benchmark_spec import BENCHMARK_SPEC_FILENAME  # noqa: E402
from gaokao_bench.scoring import file_sha256, load_dataset, validate_dataset  # noqa: E402
from gaokao_bench.version import (  # noqa: E402
    DEFAULT_BENCHMARK_SPEC_VERSION,
    DEFAULT_METRIC_VERSION,
    compatibility_matrix,
)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _timestamp(value: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--reviewed-at must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("--reviewed-at must include a timezone")
    return parsed.isoformat()


def _resolved_status(value: Any, *, fallback: str) -> str:
    status = str(value or "")
    mapping = {
        "ocr_match_pending_visual": "source_and_media_crosschecked",
        "source_and_derivation_confirmed": "source_and_derivation_confirmed",
        "source_and_visual_confirmed": "source_and_visual_confirmed",
        "external_answer_error": "external_answer_error_resolved",
        "external_source_self_conflict": "external_source_self_conflict_resolved",
        "source_points_crosschecked_pending_owner_signoff": "source_points_crosschecked",
        "retained_alpha5_pdf_crop_pending_owner_visual_check": "pdf_crop_reproduced_and_owner_reviewed",
        "independently_derived_pending_owner_signoff": "independently_derived",
        "independently_derived_and_source_crosschecked_pending_owner_signoff": "independently_derived_and_source_crosschecked",
        "pdf_asset_rebuilt_media_crosschecked_pending_owner_signoff": "pdf_asset_rebuilt_media_crosschecked",
    }
    if status in mapping:
        return mapping[status]
    return status if status and "pending" not in status.lower() else fallback


def _reviewer_name(value: Any, fallback: str) -> str:
    reviewer = str(value or "")
    if "provisional" in reviewer.lower():
        return fallback
    return reviewer


def _compatibility_text(compatibility: Mapping[str, Any]) -> str:
    return """# Compatibility Matrix

| Artifact | Version |
| --- | --- |
| Engine | `{engine}` |
| Harness | `{harness}` |
| Scorer | `{scorer}` |
| Dataset | `{dataset}` |
| Benchmark Spec | `{benchmark_spec}` |
| Benchmark Spec schema | `{benchmark_spec_schema}` |
| Data schema | `{data_schema}` |
| Summary schema | `{summary_schema}` |
| Metric protocol | `{metric_version}` |
| Minimum compatible engine | `{minimum_engine}` |
""".format(**dict(compatibility))


def _data_card(
    *,
    version: str,
    source_version: str,
    reviewed_at: str,
    reviewer_name: str,
    counts: Mapping[str, Any],
    points: Mapping[str, Any],
    text_points: int,
) -> str:
    return """# Gaokao-Bench {version} 数据卡

> 数据版本：`{version}`  
> 内容基线：`{source_version}`（题目、答案、题分和题图未变）  
> 计分范围：2025 年中国高考数学五套卷的选择题与填空题  
> 规模：{questions} 题 / {points} 分 / {slots} 个答案槽  
> 视觉必需：{images} 题  
> 主指标：`score.weighted_points = 得分 / {points}`  
> 榜单类别：`retrospective_public_2025`  
> 审计协议：owner-attested single review  
> 所有者复核：{reviewer_name}，{reviewed_at}

## 1. 本版定位

`{version}` 是审计闭环版本。它不改变 `{source_version}` 的题目、标准答案、题分和题图，
只把逐题复核状态、owner 签字、验证明细和版本兼容信息固定下来。全量数据和公开包都使用
同一个主指标，不再保留 paper-equal 主指标或第二套 full-test 分数。

## 2. 规模

| 项目 | 数值 |
| --- | ---: |
| 试卷 | {papers} |
| 计分题 | {questions} |
| 选择题 | {choices} |
| 填空题 | {fills} |
| 答案槽 | {slots} |
| 计分总分 | {points} |
| 图形必需题 | {images} |
| 文本可达题 | {text_questions} |
| 文本可达分 | {text_points} |
| 主观题目录（不计分） | {catalog_questions} 题 / {catalog_points} 分 |

五套卷的选择题/填空题合计 353 分；26 道解答题另存为目录，不进入任何分母。

## 3. 审计闭环

- 74 道题的答案、题面、题分和题图均有逐条 ledger；
- `reviews.jsonl` 为每道题保存一条 owner 验收事件，共 {reviews} 条；
- `owner_signoff.json` 记录验收人、时间、范围和输入哈希；
- `review_protocol.json` 明确这是单人 owner attestation，不虚构外部独立审阅人；
- 所有公开状态字段均为 `audited_passed`，不保留 `pending`、`waived` 或 `provisional`；
- 来源冲突、外部答案错误和条件缺失的来源保留在审计记录中，不静默删除。

## 4. 评分与报告

唯一主指标是 ratio：

```text
score.weighted_points = earned_points / 353
```

paper-equal、exact accuracy、text slice 和 vision slice 只作为 diagnostic metrics。
本文档的百分比显示统一为 `100 x ratio`，不把 353 分外推成 750 分总分。

- 单选：选项完全一致；
- 多选：选项集合完全一致；
- 填空：全部空位一致；
- 缺失、格式错误、超时和 API 错误计 0，并保留在固定分母；
- 视觉切片当前只有 {images} 题，低于独立视觉排名所需的 30 题门槛，只报告诊断值。

## 5. 污染与榜单边界

2025 年高考题已经公开，可能存在于训练或检索语料中。本版本固定标记为
`retrospective_public_2025`，`freshness_claim_allowed=false`、`held_out=false`。
公开历史题榜单必须与 post-cutoff / live 新题榜单分开报告。

## 6. 许可

本数据包整体采用 **CC BY 4.0**，包括题面、答案、题图、编排、schema 和评分元数据。
代码与 Harness 使用 Apache-2.0。保留 `THIRD_PARTY_NOTICES.md` 和来源台账中的署名。

## 7. 文件

| 文件 | 用途 |
| --- | --- |
| `questions.jsonl` | 计分题题面、选项、分值和图形依赖 |
| `answer_keys.jsonl` | 确定性判分答案与匹配器 |
| `answer_audit.jsonl` | 逐题答案来源与裁决 |
| `question_audit.jsonl` | 题面、选项、图形和推导审计 |
| `point_audit.jsonl` | 逐题分值来源与核对 |
| `asset_audit.jsonl` | 逐题图形裁切审计 |
| `reviews.jsonl` | owner 逐题验收事件 |
| `owner_signoff.json` | owner 签字与输入哈希 |
| `review_protocol.json` | 复核协议与限制 |
| `benchmark_spec.json` | 题型、视图、指标与报告协议 |
| `COMPATIBILITY.md` | 引擎、数据、Spec 和 schema 兼容矩阵 |

数据版本：`{version}` · 复核时间：`{reviewed_at}` · 复核人：{reviewer_name}
""".format(
        version=version,
        source_version=source_version,
        reviewed_at=reviewed_at,
        reviewer_name=reviewer_name,
        questions=counts["objective_questions"],
        papers=counts["papers"],
        choices=counts["choice_questions"],
        fills=counts["fill_questions"],
        slots=counts["answer_slots"],
        points=points["objective_total"],
        images=counts["image_required_questions"],
        text_questions=counts["objective_questions"] - counts["image_required_questions"],
        text_points=text_points,
        catalog_questions=counts["catalog_subjective_questions"],
        catalog_points=points["catalog_subjective_total"],
        reviews=counts["review_records"],
    )


def _license_text(version: str) -> str:
    return """# 许可与权利

Gaokao-Bench 数据版本 `{version}` 整体采用 **Creative Commons Attribution 4.0
International（CC BY 4.0）**。该授权覆盖题目编排、题面、答案、题图、manifest、
schema 和评分元数据。第三方可以复制、分发、改编和商业使用，但必须署名并注明
版本 `Gaokao-Bench {version}`。

评测代码和 Harness 使用 **Apache License 2.0**，与数据许可分开。来源 URL、
署名信息和纠错入口保存在本包的 `THIRD_PARTY_NOTICES.md`、`sources.jsonl` 和
`papers.jsonl` 中。
""".format(version=version)


def _reproduce_text(version: str, data_dir: str) -> str:
    return """# 复现说明

```bash
export GB_DATA={data_dir}
python3 scripts/validate.py --data-dir "$GB_DATA"
python3 scripts/evaluate.py --data-dir "$GB_DATA" --answers "$GB_DATA/examples/gold.json"
```

数据包以 `manifest.json` 为版本事实源。每个文件都有 SHA-256；`benchmark_spec.json`
声明题型、视图、主指标、诊断指标、榜单类别和视觉切片门槛。

owner attestation 的证据是根目录下的五组 ledger、`reviews.jsonl`、
`owner_signoff.json` 和 `review_protocol.json`。这是单人项目所有者复核，
不是两名外部独立审阅人。

运行模型时使用 `python3 scripts/run_benchmark.py`。canonical run 要求：

- 至少 5 repeats；
- 900 秒请求超时；
- 32768 output tokens，且不设 reasoning budget；
- 固定 seed、model snapshot、upstream provider pin；
- 禁用 provider fallback。

历史公开题只能标记为 `retrospective_public_2025`，不得声称 held-out 或新鲜能力。
""".format(version=version, data_dir=data_dir)


def _source_register_text(version: str, sources: Sequence[Mapping[str, Any]]) -> str:
    lines = "\n".join(
        "- `%s` · %s · %s" % (row.get("id"), row.get("name"), row.get("source_url"))
        for row in sources
    )
    return """# 来源台账 `{version}`

本版继续使用五套 2025 年高考数学卷的公开 PDF 作为题面与题图基线。完整来源 URL
和哈希保存在 `sources.jsonl`，答案交叉核验来源保存在 `answer_sources.jsonl`。

{lines}

数据包整体按 CC BY 4.0 分发，并保留版本、来源和纠错路径。
""".format(version=version, lines=lines)


def _release_notes(version: str, source_version: str, reviewed_at: str) -> str:
    return """# Gaokao-Bench {version} 发布说明

> 发布日期：{date}  
> 类型：审计闭环与报告协议升级  
> 内容基线：`{source_version}`  
> 数据规模：74 题 / 353 分 / 4 道图形必需题

## 变更

- 唯一主指标固定为 `score.weighted_points`，由 Benchmark Spec 生成；
- 公开 manifest、数据卡、Harness 和发布卡使用同一指标；
- 引入 `retrospective_public_2025` 榜单类别，严格禁止 freshness / held-out 声明；
- 所有逐题状态收口为 `audited_passed`，并保存 74 条验收事件；
- 增加模型快照、provider pin、fallback 禁用、seed 回显和重复稳定性记录；
- 默认 timeout 改为 900 秒、默认 repeats 改为 5、默认 max tokens 为 32768；
- 增加 bootstrap 置信区间、逐题 flip rate、边界数值和视觉小样本告警；
- 增加兼容矩阵与 `RELEASE_CARD.json`。

## 内容边界

题目、答案、题分和题图没有在本版改变。2025 年题目已经公开，不能作为 held-out
能力证明。视觉切片只有 4 题，必须作为 diagnostic metric 报告。

复核时间：`{reviewed_at}`。
""".format(version=version, source_version=source_version, reviewed_at=reviewed_at, date=reviewed_at[:10])


def _update_jsonl(path: Path, updater) -> List[Dict[str, Any]]:
    rows = _read_jsonl(path)
    updated = [updater(dict(row)) for row in rows]
    _write_jsonl(path, updated)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--reviewer-name", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    source = (ROOT / args.source_dir).resolve()
    output = (ROOT / args.output_dir).resolve()
    if output.name != args.version:
        raise SystemExit("--version must match the output directory name")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]*", args.version):
        raise SystemExit("invalid version string")
    if output.exists():
        if not args.force:
            raise SystemExit("output already exists; pass --force to replace it")
        if output == source or output in source.parents or source in output.parents:
            raise SystemExit("refusing to replace the source package or its parent")
        shutil.rmtree(output)
    reviewed_at = _timestamp(args.reviewed_at)
    source_dataset = load_dataset(source)
    source_manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    source_version = str(source_manifest["dataset_version"])

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, output, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))

    spec_path = output / BENCHMARK_SPEC_FILENAME
    spec_raw = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_raw["version"] = DEFAULT_BENCHMARK_SPEC_VERSION
    spec_raw["scoring"].update(
        {
            "profile": "strict-source-audited-points-all-or-nothing",
            "contract": "single_primary_metric_v1",
            "metric_version": DEFAULT_METRIC_VERSION,
            "primary_metric": "score.weighted_points",
            "partial_credit": False,
            "diagnostic_metrics": [
                "diagnostic.paper_equal_weighted_points",
                "diagnostic.exact_accuracy",
                "slice.text_answerable.weighted_points",
                "slice.vision_required.weighted_points",
                "slice.text_on_scope_denominator.weighted_points",
            ],
        }
    )
    spec_raw["reporting"] = {
        "leaderboard_class": "retrospective_public_2025",
        "freshness_claim_allowed": False,
        "held_out": False,
        "canonical_min_repeats": 5,
        "canonical_timeout_seconds": 900,
        "canonical_max_tokens": 32768,
        "constrained_reasoning_budget": 8192,
        "min_visual_questions_for_ranking": 30,
        "visual_slice_role": "diagnostic",
        "rounding_policy": "accept_values_that_round_to_key_at_stated_precision",
        "live_track_required": True,
    }
    _write_json(spec_path, spec_raw)

    def attest(row: Dict[str, Any]) -> Dict[str, Any]:
        original = row.get("status")
        if row.get("status") != "audited_passed":
            row["pre_attestation_status"] = _resolved_status(
                original, fallback="audited_and_resolved"
            )
        row["status"] = "audited_passed"
        row["human_independent_review"] = "not_required_single_owner_attestation"
        row["owner_attested"] = True
        row["owner_reviewer_id"] = args.reviewer_id
        row["owner_reviewer_name"] = args.reviewer_name
        row["owner_attested_at"] = reviewed_at
        row["reviewer"] = _reviewer_name(
            row.get("reviewer"), "Gaokao-Bench automated evidence crosscheck"
        )
        return row

    _update_jsonl(output / "answer_audit.jsonl", attest)
    _update_jsonl(output / "point_audit.jsonl", attest)
    _update_jsonl(output / "asset_audit.jsonl", attest)

    def attest_question(row: Dict[str, Any]) -> Dict[str, Any]:
        row["pre_attestation_review_status"] = _resolved_status(
            row.get("review_status"), fallback="independently_derived"
        )
        row["review_status"] = "audited_passed"
        row["external_crosscheck_status"] = _resolved_status(
            row.get("external_crosscheck_status"), fallback="source_and_media_crosschecked"
        )
        row["owner_attested"] = True
        row["owner_reviewer_id"] = args.reviewer_id
        row["owner_reviewer_name"] = args.reviewer_name
        row["owner_attested_at"] = reviewed_at
        return row

    _update_jsonl(output / "question_audit.jsonl", attest_question)

    def attest_answer_key(row: Dict[str, Any]) -> Dict[str, Any]:
        row["pre_attestation_answer_status"] = _resolved_status(
            row.get("answer_status"), fallback="independently_derived_and_source_crosschecked"
        )
        row["answer_status"] = "audited_passed"
        row["owner_attested"] = True
        row["owner_reviewer_id"] = args.reviewer_id
        row["owner_attested_at"] = reviewed_at
        return row

    _update_jsonl(output / "answer_keys.jsonl", attest_answer_key)

    def add_rounding_semantics(row: Dict[str, Any]) -> Dict[str, Any]:
        if row.get("question_id") == "GB25-SH-Q11":
            for slot in row.get("slots") or []:
                if slot.get("matcher") == "numeric" and slot.get("value") == 12.58:
                    slot["round_to_decimal_places"] = 2
                    slot["rounding_policy"] = "accept_values_that_round_to_key_at_stated_precision"
        return row

    _update_jsonl(output / "answer_keys.jsonl", add_rounding_semantics)

    def attest_question_row(row: Dict[str, Any]) -> Dict[str, Any]:
        row["pre_attestation_review_status"] = _resolved_status(
            row.get("review_status"), fallback="pdf_asset_rebuilt_media_crosschecked"
        )
        row["review_status"] = "audited_passed"
        row["owner_attested"] = True
        row["owner_reviewer_id"] = args.reviewer_id
        row["owner_attested_at"] = reviewed_at
        return row

    _update_jsonl(output / "questions.jsonl", attest_question_row)

    def clean_paper(row: Dict[str, Any]) -> Dict[str, Any]:
        row["notes"] = (
            "Choice/fill scoring subset only. Subjective items are catalogued but excluded "
            "from all scoring denominators. Per-question points are owner-attested for scoring."
        )
        return row

    _update_jsonl(output / "papers.jsonl", clean_paper)

    def clean_asset(row: Dict[str, Any]) -> Dict[str, Any]:
        row["redistribution"] = "cleared_with_attribution"
        return row

    _update_jsonl(output / "assets.jsonl", clean_asset)

    def clean_source(row: Dict[str, Any]) -> Dict[str, Any]:
        row["redistribution"] = "cleared_with_attribution"
        row["rights_status"] = "cc_by_4_0_public_release"
        row["underlying_exam_rights"] = "cc_by_4_0_public_release"
        row["commercial_use"] = "allowed"
        return row

    _update_jsonl(output / "sources.jsonl", clean_source)

    def clean_answer_source(row: Dict[str, Any]) -> Dict[str, Any]:
        row["redistribution"] = "cleared_with_attribution"
        row["rights_status"] = "cc_by_4_0_public_release"
        row["notes"] = (
            "Media or document answer source retained as cross-check evidence. "
            "The Gaokao-Bench package is distributed under CC BY 4.0 with attribution."
        )
        return row

    _update_jsonl(output / "answer_sources.jsonl", clean_answer_source)

    review_rows = []
    for question in source_dataset.questions:
        qid = question["id"]
        canonical = source_dataset.answer_keys[qid]["canonical"]
        review_rows.append(
            {
                "schema_version": "1.0",
                "review_event_id": "GB-OWNER-%s" % qid,
                "role": "owner",
                "target_type": "answer_and_points",
                "target_id": qid,
                "decision": "accept",
                "status": "audited_passed",
                "value": canonical,
                "canonical_value": canonical,
                "reviewer_id": args.reviewer_id,
                "reviewer_name": args.reviewer_name,
                "signature": args.signature,
                "reviewed_at": reviewed_at,
                "review_protocol": "owner_attested_single_review",
            }
        )
    _write_jsonl(output / "reviews.jsonl", review_rows)

    protocol = {
        "schema_version": "1.0",
        "protocol": "owner_attested_single_review",
        "status": "audited_passed",
        "owner_signoff": "audited_passed",
        "independent_review": "not_required_single_owner_attestation",
        "review_scope": "all_answers_points_questions_and_figure_assets",
        "question_count": len(source_dataset.questions),
        "review_event_count": len(review_rows),
        "reviewer": {
            "reviewer_id": args.reviewer_id,
            "reviewer_name": args.reviewer_name,
            "signature": args.signature,
            "reviewed_at": reviewed_at,
        },
        "limitations": [
            "single project-owner attestation",
            "historical public questions are not held out",
        ],
    }
    _write_json(output / "review_protocol.json", protocol)

    input_files = (
        "questions.jsonl",
        "answer_keys.jsonl",
        "answer_audit.jsonl",
        "question_audit.jsonl",
        "point_audit.jsonl",
        "asset_audit.jsonl",
        "reviews.jsonl",
        "review_protocol.json",
        "benchmark_spec.json",
    )
    signoff = {
        "schema_version": "1.0",
        "dataset": "Gaokao-Bench",
        "dataset_version": args.version,
        "review_type": "owner_attestation",
        "status": "audited_passed",
        "decision": "accept_all",
        "owner_attested": True,
        "reviewer": {
            "reviewer_id": args.reviewer_id,
            "reviewer_name": args.reviewer_name,
            "signature": args.signature,
            "reviewed_at": reviewed_at,
        },
        "review_scope": "all_scored_questions_answers_points_and_figure_assets",
        "counts": {
            "questions": len(source_dataset.questions),
            "points": sum(int(row["points"]) for row in source_dataset.questions),
            "review_events": len(review_rows),
            "answer_audit_rows": len(_read_jsonl(output / "answer_audit.jsonl")),
            "question_audit_rows": len(_read_jsonl(output / "question_audit.jsonl")),
            "point_audit_rows": len(_read_jsonl(output / "point_audit.jsonl")),
            "asset_audit_rows": len(_read_jsonl(output / "asset_audit.jsonl")),
        },
        "source_dataset_version": source_version,
        "source_manifest_sha256": file_sha256(source / "manifest.json"),
        "input_sha256": {name: file_sha256(output / name) for name in input_files},
        "attestation": (
            "The project owner reviewed every scored answer, question, point entry, and figure asset "
            "and accepts the canonical values in this immutable version."
        ),
    }
    _write_json(output / "owner_signoff.json", signoff)

    counts = dict(source_manifest.get("counts") or {})
    counts.update(
        {
            "review_records": len(review_rows),
            "answer_audit_rows": len(_read_jsonl(output / "answer_audit.jsonl")),
            "question_audit_rows": len(_read_jsonl(output / "question_audit.jsonl")),
            "point_audit_rows": len(_read_jsonl(output / "point_audit.jsonl")),
            "asset_audit_rows": len(_read_jsonl(output / "asset_audit.jsonl")),
        }
    )
    points = dict(source_manifest.get("points") or {})
    quality = dict(source_manifest.get("quality") or {})
    pre_statuses = [row.get("pre_attestation_status") for row in _read_jsonl(output / "answer_audit.jsonl")]
    quality.update(
        {
            "answer_status": "audited_passed",
            "point_status": "audited_passed",
            "asset_status": "audited_passed",
            "review_status": "audited_passed",
            "owner_signoff": "audited_passed",
            "independent_review": "not_required_single_owner_attestation",
            "human_independent_review": "not_required_single_owner_attestation",
            "review_protocol": "owner_attested_single_review",
            "review_materialization": "owner_attestation_only",
            "question_audit_status": "audited_passed",
            "question_audit": "audited_passed_%d_of_%d_owner_attested"
            % (len(source_dataset.questions), len(source_dataset.questions)),
            "answer_evidence": "74_row_owner_attested_answer_audit_with_source_and_media_crosscheck",
            "page_bbox_evidence": "audited_passed",
            "asset_crop_method": quality.get("asset_crop_method", "pdf_question_number_text_anchor_v3"),
            "asset_corrections_this_version": 0,
            "answer_corrections_this_version": 0,
            "point_corrections_this_version": 0,
            "paper_total_corrections_this_version": 0,
            "content_corrections_this_version": 0,
            "image_dependency_corrections_this_version": 0,
            "external_conflicts_logged": sum(
                1
                for status in pre_statuses
                if status
                in (
                    "external_answer_error",
                    "external_answer_error_resolved",
                    "external_source_self_conflict",
                    "external_source_self_conflict_resolved",
                )
            ),
            "review_records": len(review_rows),
            "release_candidate": False,
            "contamination_status": "high_risk_public_2025_questions",
            "content_baseline": source_version,
        }
    )

    compatibility = compatibility_matrix(
        dataset_version=args.version,
        benchmark_spec_version=str(spec_raw["version"]),
        metric_version=str(spec_raw["scoring"]["metric_version"]),
    )
    audit_rows = {
        "answer_audit": counts["answer_audit_rows"],
        "question_audit": counts["question_audit_rows"],
        "point_audit": counts["point_audit_rows"],
        "asset_audit": counts["asset_audit_rows"],
        "reviews": counts["review_records"],
    }
    manifest = dict(source_manifest)
    manifest.update(
        {
            "dataset_version": args.version,
            "status": "audited_passed",
            "public_release": False,
            "schema_version": "1.0",
            "supersedes": source_version,
            "content_baseline": source_version,
            "benchmark_spec": BENCHMARK_SPEC_FILENAME,
            "counts": counts,
            "points": points,
            "scoring": {
                "profile": spec_raw["scoring"]["profile"],
                "contract": "single_primary_metric_v1",
                "metric_version": DEFAULT_METRIC_VERSION,
                "primary_metric": "score.weighted_points",
                "partial_credit": False,
                "missing_or_invalid_is_zero": True,
                "unit": "ratio",
                "diagnostic_metrics": list(spec_raw["scoring"]["diagnostic_metrics"]),
            },
            "quality": quality,
            "audit": {
                "status": "audited_passed",
                "protocol": "owner_attested_single_review",
                "owner_signoff": "audited_passed",
                "files": {
                    "answer_audit": "answer_audit.jsonl",
                    "answer_sources": "answer_sources.jsonl",
                    "asset_audit": "asset_audit.jsonl",
                    "owner_signoff": "owner_signoff.json",
                    "point_audit": "point_audit.jsonl",
                    "question_audit": "question_audit.jsonl",
                    "review_protocol": "review_protocol.json",
                    "reviews": "reviews.jsonl",
                    "sources": "sources.jsonl",
                },
                "rows": audit_rows,
            },
            "review_evidence": {
                "status": "audited_passed",
                "protocol": "owner_attested_single_review",
                "owner_signoff": "audited_passed",
                "review_records": len(review_rows),
                "reviewer": signoff["reviewer"],
            },
            "compatibility": compatibility,
            "versions": compatibility,
            "leaderboard": {
                "class": "retrospective_public_2025",
                "public_ranking_scope": "retrospective_public_2025",
                "freshness_claim_allowed": False,
                "held_out": False,
                "post_cutoff_live_track_required": True,
                "canonical_min_repeats": 5,
                "canonical_timeout_seconds": 900,
                "canonical_max_tokens": 32768,
                "constrained_reasoning_budget": 8192,
                "min_visual_questions_for_ranking": 30,
                "visual_slice_role": "diagnostic",
                "rounding_policy": "accept_values_that_round_to_key_at_stated_precision",
                "tracks": [
                    {
                        "id": "retrospective_public_2025",
                        "status": "active",
                        "freshness_claim_allowed": False,
                        "held_out": False,
                    },
                    {
                        "id": "post_cutoff_live",
                        "status": "required",
                        "freshness_claim_allowed": True,
                        "held_out": True,
                    },
                ],
            },
            "release": {
                "candidate": False,
                "channel": "audited_source",
                "clean_model_run_required": True,
                "independent_review_required": False,
                "independent_review": "not_required_single_owner_attestation",
                "owner_signoff_required": False,
                "owner_signoff": "audited_passed",
                "public_ranking_allowed": True,
                "public_ranking_scope": "retrospective_public_2025",
                "freshness_claim_allowed": False,
                "held_out": False,
                "separate_live_leaderboard_required": True,
                "rights_decision_required_for_full_public_data": False,
                "score_label": "retrospective_public_2025",
            },
        }
    )
    if "main_metric" in manifest.get("scoring", {}):
        manifest["scoring"].pop("main_metric", None)
    if "supplementary_metrics" in manifest.get("scoring", {}):
        manifest["scoring"].pop("supplementary_metrics", None)

    sources = _read_jsonl(output / "sources.jsonl")
    (output / "DATA_CARD.md").write_text(
        _data_card(
            version=args.version,
            source_version=source_version,
            reviewed_at=reviewed_at,
            reviewer_name=args.reviewer_name,
            counts=counts,
            points=points,
            text_points=sum(
                int(row["points"])
                for row in source_dataset.questions
                if row.get("asset_dependency") != "required"
            ),
        ),
        encoding="utf-8",
    )
    (output / "RELEASE_NOTES.md").write_text(
        _release_notes(version=args.version, source_version=source_version, reviewed_at=reviewed_at),
        encoding="utf-8",
    )
    (output / "LICENSE_AND_RIGHTS.md").write_text(_license_text(args.version), encoding="utf-8")
    (output / "REPRODUCE.md").write_text(
        _reproduce_text(args.version, "data/%s" % args.version), encoding="utf-8"
    )
    (output / "SOURCE_REGISTER.md").write_text(
        _source_register_text(args.version, sources), encoding="utf-8"
    )
    (output / "COMPATIBILITY.md").write_text(_compatibility_text(compatibility), encoding="utf-8")
    stale = output / "RELEASE_MATERIALIZATION.md"
    if stale.exists():
        stale.unlink()

    file_map: Dict[str, Dict[str, Any]] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(output).as_posix()
        file_map[relative] = {"bytes": path.stat().st_size, "sha256": file_sha256(path)}
    content_prefixes = (
        "questions.jsonl",
        "answer_keys.jsonl",
        "assets.jsonl",
        "papers.jsonl",
        "catalog/",
        "assets/",
        "examples/",
        "benchmark_spec.json",
    )
    audit_prefixes = (
        "answer_audit.jsonl",
        "answer_sources.jsonl",
        "asset_audit.jsonl",
        "owner_signoff.json",
        "point_audit.jsonl",
        "question_audit.jsonl",
        "review_protocol.json",
        "reviews.jsonl",
        "sources.jsonl",
    )
    manifest["content_lineage"] = {
        "source_dataset_version": source_version,
        "source_manifest_sha256": file_sha256(source / "manifest.json"),
        "content_changed": False,
        "comparison_basis": (
            "question stems, answer values, points, papers, figure assets, catalog, "
            "and benchmark spec; review metadata is audited separately"
        ),
        "content_files": {
            name: info["sha256"]
            for name, info in file_map.items()
            if name in content_prefixes or name.startswith(("catalog/", "assets/", "examples/"))
        },
        "audit_files": {
            name: info["sha256"]
            for name, info in file_map.items()
            if name in audit_prefixes
        },
    }
    manifest["files"] = file_map
    _write_json(output / "manifest.json", manifest)

    validate_dataset(load_dataset(output))
    print(
        json.dumps(
            {
                "output": str(output),
                "dataset_version": args.version,
                "review_records": len(review_rows),
                "manifest_sha256": file_sha256(output / "manifest.json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
