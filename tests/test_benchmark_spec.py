from copy import deepcopy

import pytest

from gaokao_bench.benchmark_spec import BenchmarkSpecError, parse_benchmark_spec


def _spec():
    return {
        "schema_version": "1.0",
        "id": "scoring-contract-test",
        "version": "1.0",
        "subject": "数学",
        "years": [2026],
        "task_types": {
            "single_choice": {
                "shape": "choice",
                "min_selections": 1,
                "max_selections": 1,
            },
            "fill_blank": {"shape": "slots"},
            "catalog_subjective": {"shape": "rubric"},
        },
        "choice_labels": ["A", "B", "C", "D"],
        "prompt": {"version": "test-v1", "system": "只输出 JSON"},
        "scoring": {
            "profile": "deterministic",
            "metric_version": "1.0",
        },
        "expected": {"paper_count": 1},
    }


def test_task_type_eligibility_is_inferred_for_legacy_specs():
    spec = parse_benchmark_spec(_spec())
    assert spec.scoring.primary_task_types == ("single_choice", "fill_blank")
    assert spec.scoring.catalog_task_types == ("catalog_subjective",)
    assert spec.scoring.credit_policy == "all_or_nothing"
    assert spec.scoring.process_policy == "excluded_from_primary"


def test_explicit_primary_and_catalog_scopes_are_disjoint():
    raw = _spec()
    raw["scoring"].update(
        {
            "primary_task_types": ["single_choice", "fill_blank"],
            "catalog_task_types": ["catalog_subjective"],
        }
    )
    spec = parse_benchmark_spec(raw)
    assert spec.scoring.primary_task_types == ("single_choice", "fill_blank")
    assert spec.scoring.catalog_task_types == ("catalog_subjective",)

    duplicate = deepcopy(raw)
    duplicate["scoring"]["catalog_task_types"].append("single_choice")
    with pytest.raises(BenchmarkSpecError, match="must not overlap"):
        parse_benchmark_spec(duplicate)


def test_primary_scoring_rejects_rubric_task_types():
    raw = _spec()
    raw["scoring"]["primary_task_types"] = ["catalog_subjective"]
    with pytest.raises(BenchmarkSpecError, match="choice or slots"):
        parse_benchmark_spec(raw)


def test_every_declared_task_type_is_primary_or_catalog():
    raw = _spec()
    raw["scoring"].update(
        {
            "primary_task_types": ["single_choice", "fill_blank"],
            "catalog_task_types": ["catalog_subjective"],
        }
    )
    raw["task_types"]["unused"] = {"shape": "choice", "min_selections": 1, "max_selections": 1}
    with pytest.raises(BenchmarkSpecError, match="either primary or catalog"):
        parse_benchmark_spec(raw)


def test_canonical_contract_requires_supported_single_full_credit_diagnostic():
    raw = _spec()
    raw["scoring"].update(
        {
            "contract": "single_primary_metric_v1",
            "diagnostic_metrics": [
                "diagnostic.full_credit_rate",
                "slice.text_answerable.weighted_points",
                "slice.vision_required.weighted_points",
            ],
        }
    )
    spec = parse_benchmark_spec(raw)
    assert "diagnostic.full_credit_rate" in spec.scoring.diagnostic_metrics

    duplicate = deepcopy(raw)
    duplicate["scoring"]["diagnostic_metrics"].append("diagnostic.exact_accuracy")
    with pytest.raises(BenchmarkSpecError, match="at most one full-credit"):
        parse_benchmark_spec(duplicate)

    missing_slice = deepcopy(raw)
    missing_slice["scoring"]["diagnostic_metrics"].remove(
        "slice.vision_required.weighted_points"
    )
    with pytest.raises(BenchmarkSpecError, match="missing diagnostic slices"):
        parse_benchmark_spec(missing_slice)

    renamed_primary = deepcopy(raw)
    renamed_primary["scoring"]["primary_metric"] = "score.renamed"
    with pytest.raises(BenchmarkSpecError, match="primary_metric=score.weighted_points"):
        parse_benchmark_spec(renamed_primary)


def test_partial_credit_and_process_scoring_are_not_silently_enabled():
    raw = _spec()
    raw["scoring"]["partial_credit"] = True
    with pytest.raises(BenchmarkSpecError, match="partial_credit must be false"):
        parse_benchmark_spec(raw)

    raw = _spec()
    raw["scoring"]["process_policy"] = "included_in_primary"
    with pytest.raises(BenchmarkSpecError, match="process_policy"):
        parse_benchmark_spec(raw)
