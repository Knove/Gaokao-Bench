"""Compatibility specification for the already-published 0.0.1 data package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..benchmark_spec import BenchmarkSpec, parse_benchmark_spec


SYSTEM_PROMPT = """你是一名严谨的数学解题者。只依据用户给出的题目文字和随附图形作答。

要求：
1. 不猜测题目来自哪个年份、地区、考试或题号，也不使用外部检索。
2. 不输出解题过程、解释、Markdown 代码块或多余文字。
3. 只输出一个合法 JSON 对象：{\"answer\": ...}
4. 单选题的 answer 是一个大写字母，例如 \"C\"。
5. 多选题的 answer 是升序拼接的大写字母，例如 \"ACD\"。
6. 单空题的 answer 是字符串；多空题的 answer 是按空位顺序排列的 JSON 数组。
7. 数学表达式使用题面中的 Unicode 记法；分数、根式、区间、向量按题意简洁书写。
"""


LEGACY_SPEC_RAW: dict[str, Any] = {
    "schema_version": "1.0",
    "id": "gaokao-math-2025-objective",
    "version": "1.0",
    "subject": "数学",
    "years": [2025],
    "task_types": {
        "single_choice": {"shape": "choice", "min_selections": 1, "max_selections": 1},
        "multi_choice": {"shape": "choice", "min_selections": 2},
        "fill_blank": {"shape": "slots"},
    },
    "choice_labels": ["A", "B", "C", "D"],
    "audit_profile": "legacy_2025_math",
    "validators": ["numeric", "interval", "set_tokens", "vector_linear"],
    "custom_validators": ["beijing_q13_trig_pair"],
    "expected": {
        "question_count": 74,
        "paper_count": 5,
        "total_points": 353,
        "paper_points": {
            "GB25-N1": 73,
            "GB25-N2": 73,
            "GB25-BJ": 60,
            "GB25-SH": 72,
            "GB25-TJ": 75,
        },
        "full_paper_question_count": 100,
        "full_paper_points": 750,
        "task_type_counts": {
            "single_choice": 39,
            "multi_choice": 6,
            "fill_blank": 29,
        },
        "catalog_question_count": 26,
        "catalog_points": 397,
        "required_image_ids": [
            "GB25-N1-Q06",
            "GB25-BJ-Q14",
            "GB25-SH-Q11",
            "GB25-TJ-Q03",
        ],
    },
    "views": [
        {
            "id": "full",
            "description": "All 74 scored objective questions and their 353 exam points.",
            "selector": {"all": True},
        }
    ],
    "prompt": {
        "version": "gbmath-objective-anonymous-v2",
        "answer_schema": "gaokao-answer-v1",
        "system": SYSTEM_PROMPT,
        "image_crops": {
            "GB25-N1-Q06": [0, 270, 841, 590],
            "GB25-BJ-Q14": [0, 190, 836, 505],
            "GB25-SH-Q11": [0, 185, 841, 430],
            "GB25-TJ-Q03": [0, 45, 841, 375],
        },
        "image_pixel_sha256": {
            "GB25-N1-Q06": "cf770f349f42ff9de45a0b135278bc1100ccaec5b843f5c31e2bf148daf3bf90",
            "GB25-BJ-Q14": "01e2ab1ae446db206f80b029707435bbf2b5d7eb2a2336d4ab62a6d51fe75415",
            "GB25-SH-Q11": "d7f127f5a89b886116253530d360d644d9f90fbaca83552ed86b379efbedf521",
            "GB25-TJ-Q03": "813b28d19960b19ebcd1dca27726d8913f29c197cd91f8976baa42dfbea0e7e5",
        },
    },
    "scoring": {
        "profile": "strict-source-audited-points-all-or-nothing",
        "metric_version": "1.0",
        "primary_metric": "score.weighted_points",
        "partial_credit": False,
    },
}


LEGACY_SPEC = parse_benchmark_spec(LEGACY_SPEC_RAW)


def matches_manifest(manifest: Mapping[str, Any]) -> bool:
    scope = manifest.get("scope") if isinstance(manifest.get("scope"), Mapping) else {}
    papers = scope.get("papers")
    included_types = scope.get("included_types")
    return (
        manifest.get("dataset") == "Gaokao-Bench"
        and scope.get("subject") == "数学"
        and scope.get("year") == 2025
        and scope.get("scoring_scope") == "choice_and_fill_only"
        and isinstance(included_types, list)
        and sorted(str(item) for item in included_types)
        == ["fill_blank", "multi_choice", "single_choice"]
        and isinstance(papers, list)
        and len(papers) == 5
    )
