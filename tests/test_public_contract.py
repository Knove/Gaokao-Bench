import json
import re
from pathlib import Path
from types import SimpleNamespace

from gaokao_bench.cli import build_parser
from gaokao_bench.benchmark_spec import parse_benchmark_spec
from gaokao_bench.harness import (
    build_answer_instruction,
    build_prompt,
    classify_answer_format,
    parse_model_response,
    render_system_prompt,
)
from gaokao_bench.public_data import _dataset_card
from gaokao_bench.owner_review import (
    _review_items,
    _source_image_url,
    _uses_full_paper_review,
)
from gaokao_bench.reporting import (
    compact_evaluation_summary,
    compact_run_summary,
    render_evaluation_summary,
    render_run_summary,
)
from gaokao_bench.scoring import (
    EXPLICIT_SCORING_POLICY_FIELDS,
    _catalog_task_type_errors,
    _requires_explicit_scoring_policy,
    _scoring_contract_errors,
    parse_interval,
    parse_numeric_expression,
    parse_set_tokens,
    public_scoring_contract,
)
from gaokao_bench.scoring import PUBLIC_REQUIRED_FILES
from gaokao_bench.version import ENGINE_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_owner_review_expands_to_the_catalogued_full_paper():
    dataset = SimpleNamespace(
        questions=[{"id": "Q01"}, {"id": "Q02"}],
        catalog_subjective=[{"id": "D03"}],
        manifest={"quality": {"owner_review_scope": "full_paper"}},
    )
    assert _uses_full_paper_review(dataset) is True
    assert [item["id"] for item in _review_items(dataset)] == ["Q01", "Q02", "D03"]


def test_owner_review_keeps_scored_only_scope_for_legacy_packages():
    dataset = SimpleNamespace(
        questions=[{"id": "Q01"}],
        catalog_subjective=[{"id": "D02"}],
        manifest={"quality": {}},
    )
    assert _uses_full_paper_review(dataset) is False
    assert [item["id"] for item in _review_items(dataset)] == ["Q01"]


def test_owner_review_maps_question_numbers_to_answer_images():
    source = {
        "answer_image_urls": ["https://example.test/page-1.png", "https://example.test/page-2.png"],
        "answer_image_pages": {"13": 2},
    }
    assert _source_image_url(source, "完整解析图；第13题") == "https://example.test/page-2.png"
    assert _source_image_url(source, "第14题") == ""


def test_public_numeric_parser_contract():
    assert parse_numeric_expression("7/4") == 1.75
    assert parse_numeric_expression(r"\frac{7}{4}") == 1.75
    assert abs(parse_numeric_expression("3√2") - 4.242640687119286) < 1e-12
    assert abs(parse_numeric_expression("∛(3/2)") - (3 / 2) ** (1 / 3)) < 1e-12
    assert abs(parse_numeric_expression(r"\sqrt[3]{3/2}") - (3 / 2) ** (1 / 3)) < 1e-12
    assert abs(parse_numeric_expression("cbrt(3/2)") - (3 / 2) ** (1 / 3)) < 1e-12


def test_public_interval_parser_contract():
    assert parse_interval("[4,5]") == (4.0, 5.0, True, True)
    assert parse_interval("(1,3)") == (1.0, 3.0, False, False)
    assert parse_interval("{x | 1 < x <= 3}") == (1.0, 3.0, False, True)


def test_set_answer_accepts_equivalent_wire_shapes():
    assert parse_set_tokens("②③") == ("2", "3")
    assert parse_set_tokens("③、②") == ("2", "3")
    assert parse_set_tokens(["②", "③"]) == ("2", "3")
    assert parse_set_tokens(("3", "2")) == ("2", "3")


def _strict_math_spec():
    raw = json.loads((ROOT / "specs" / "2026-math-n1.json").read_text(encoding="utf-8"))
    return parse_benchmark_spec(raw["benchmark_spec"])


def test_release_scoring_metadata_is_generated_from_the_benchmark_spec():
    spec = _strict_math_spec()
    scoring = public_scoring_contract(spec)
    assert scoring["contract"] == "single_primary_metric_v1"
    assert scoring["primary_metric"] == "score.weighted_points"
    assert scoring["primary_task_types"] == list(spec.scoring.primary_task_types)
    assert scoring["catalog_task_types"] == list(spec.scoring.catalog_task_types)
    assert scoring["partial_credit"] is False
    assert (
        _scoring_contract_errors(
            spec,
            scoring,
            label="public manifest",
            require_policy_fields=True,
        )
        == []
    )

    for field in EXPLICIT_SCORING_POLICY_FIELDS:
        incomplete = dict(scoring)
        incomplete.pop(field)
        errors = _scoring_contract_errors(
            spec,
            incomplete,
            label="public manifest",
            require_policy_fields=True,
        )
        assert "public manifest scoring.%s is required" % field in errors

    mismatched = dict(scoring)
    mismatched["partial_credit"] = True
    errors = _scoring_contract_errors(
        spec,
        mismatched,
        label="RELEASE_CARD",
        require_policy_fields=True,
    )
    assert "RELEASE_CARD scoring.partial_credit differs from benchmark spec: True != False" in errors


def test_strict_scoring_metadata_applies_only_to_new_dataset_versions():
    legacy = {
        "dataset_version": "0.0.4",
        "compatibility": {"dataset": "0.0.4", "engine": "0.1.8"},
    }
    current = {
        "dataset_version": "0.1.0-alpha.2026-n1",
        "compatibility": {"dataset": "0.1.0-alpha.2026-n1", "engine": "0.1.8"},
    }
    old_engine = {
        "dataset_version": "0.1.0-alpha.2026-n1",
        "compatibility": {"dataset": "0.1.0-alpha.2026-n1", "engine": "0.1.7"},
    }
    missing_engine = {
        "dataset_version": "0.1.0-alpha.2026-n1",
        "compatibility": {"dataset": "0.1.0-alpha.2026-n1"},
    }
    versions_only = {
        "dataset_version": "0.1.0-alpha.2026-n1",
        "versions": {"dataset": "0.1.0-alpha.2026-n1", "engine": "0.1.7"},
    }
    historical_alpha = {
        "dataset_version": "0.1.0-alpha.6.1",
        "scoring": {"main_metric": "mean of five paper point accuracies"},
    }
    modern_engine_with_legacy_scoring = {
        "dataset_version": "0.1.0-alpha.6.1",
        "compatibility": {"engine": "0.1.8"},
        "scoring": {"main_metric": "mean of five paper point accuracies"},
    }
    assert _requires_explicit_scoring_policy(legacy) is False
    assert _requires_explicit_scoring_policy(current) is True
    assert _requires_explicit_scoring_policy(old_engine) is False
    assert _requires_explicit_scoring_policy(missing_engine) is True
    assert _requires_explicit_scoring_policy(versions_only) is False
    assert _requires_explicit_scoring_policy(historical_alpha) is False
    assert _requires_explicit_scoring_policy(modern_engine_with_legacy_scoring) is True

    error = "catalogued subjective items require explicit scoring.catalog_task_types"
    assert (
        _catalog_task_type_errors(
            [{"id": "D01"}], [], require_explicit=True
        )
        == [error]
    )
    assert _catalog_task_type_errors([{"id": "D01"}], [], require_explicit=False) == []


def test_prompt_declares_task_specific_answer_format():
    circled = {
        "type": "fill_blank",
        "answer_slot_count": 1,
        "stem": "其中正确结论的序号是____。① 甲 ② 乙 ③ 丙",
        "options": [],
    }
    prompt = build_prompt(circled)
    assert classify_answer_format(circled) == "circled_set"
    assert "序号填空题" in prompt
    assert "不要拆成数组" in prompt
    assert '{"answer":"<题面序号串>"}' in prompt
    assert '"answer":"②③"' not in prompt
    assert "canonical" not in prompt

    single = {
        "type": "single_choice",
        "answer_slot_count": 1,
        "stem": "下列说法正确的是（ ）",
        "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"}],
    }
    assert classify_answer_format(single) == "single_choice"
    assert '{"answer":"<单个选项字母>"}' in build_answer_instruction(single)
    assert '"answer":"A"' not in build_prompt(single)

    multi = {
        "type": "multi_choice",
        "answer_slot_count": 1,
        "stem": "下列说法正确的是（ ）",
        "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}, {"label": "D"}],
    }
    assert "多选题" in build_answer_instruction(multi)
    assert classify_answer_format(multi) == "multi_choice"
    assert '{"answer":"<选项字母串>"}' in build_answer_instruction(multi)

    multi_slot = {
        "type": "fill_blank",
        "answer_slot_count": 2,
        "stem": "两个空：____，____。",
        "options": [],
    }
    assert classify_answer_format(multi_slot) == "multi_slot"
    assert "2 个字符串" in build_answer_instruction(multi_slot)
    assert '"<第1空>"' in build_answer_instruction(multi_slot)


def test_system_prompt_answer_examples_are_neutralized():
    system = (
        '4. 单选题的 answer 是一个大写字母，例如 "C"。\n'
        '5. 多选题的 answer 是升序拼接的大写字母，例如 "ACD"。\n'
        '6. 填空题的 answer 例如 "7/4"。'
    )
    rendered = render_system_prompt(system)
    assert '"C"' not in rendered
    assert '"ACD"' not in rendered
    assert '"7/4"' not in rendered
    assert '"<单个选项字母>"' in rendered
    assert '"<选项字母串>"' in rendered
    assert '"<答案>"' in rendered


def test_parser_is_total_for_common_and_arbitrary_responses():
    cases = {
        '{"answer":"②③"}': ("ok_json", "②③"),
        '{"answer":["②","③"]}': ("ok_json", ["②", "③"]),
        "```json\n{\"answer\":\"A\"}\n```": ("ok_json", "A"),
        "最终答案：ACD": ("ok_labeled", "ACD"),
        "ACD": ("ok_choice", "ACD"),
    }
    for raw, (status, answer) in cases.items():
        parsed = parse_model_response(raw)
        assert parsed["status"] == status
        assert parsed["answer"] == answer
    alphabet = '{}[]"answer:：最终答案ACD ①②③\n`'
    for index in range(500):
        raw = "".join(alphabet[(index * 17 + offset * 31) % len(alphabet)] for offset in range(index % 37))
        parsed = parse_model_response(raw)
        assert parsed["status"] in {"ok_json", "ok_labeled", "ok_choice", "parse_error"}


def test_evaluation_reporting_keeps_ids_out_of_the_human_and_compact_views():
    report = {
        "dataset": "Gaokao-Bench",
        "dataset_version": "0.0.4",
        "scoring_profile": "strict-source-audited-points-all-or-nothing",
        "unexpected_answer_ids": [],
        "summary": {
            "earned_points": 353,
            "possible_points": 353,
            "correct_questions": 74,
            "total_questions": 74,
            "failure_counts": {"correct": 74},
            "primary_metric": {
                "id": "score.weighted_points",
                "value": 1.0,
                "numerator": 353,
                "denominator": 353,
                "scope": "full_dataset",
                "coverage": {"selected_question_ids": ["GB25-BJ-Q01"]},
            },
            "slices": {},
            "papers": {},
        },
    }
    human = render_evaluation_summary(report)
    compact = compact_evaluation_summary(report)
    assert "353 / 353 points" in human
    assert "GB25-BJ-Q01" not in human
    assert "selected_question_ids" not in json.dumps(compact)


def test_evaluate_cli_parser_contract():
    args = build_parser().parse_args(
        ["evaluate", "--answers", "submission.json", "--report", "report.json", "--json"]
    )
    assert args.report == "report.json"
    assert args.json is True
    args = build_parser().parse_args(
        ["evaluate", "--answers", "submission.json", "--output", "legacy.json"]
    )
    assert args.report == "legacy.json"


def test_run_reporting_keeps_scope_ids_out_of_the_summary():
    summary = {
        "dataset": "Gaokao-Bench",
        "dataset_version": "0.0.4",
        "result_status": "pilot",
        "run_id": "run-1",
        "provider": "mock",
        "model": "mock",
        "modality": "text",
        "repeats": 1,
        "scoring_policy": {
            "credit_policy": "all_or_nothing",
            "process_policy": "excluded_from_primary",
        },
        "scope": {
            "coverage_label": "full",
            "kind": "full_dataset",
            "selected_questions": 74,
            "selected_possible_points": 353,
            "coverage": {"selected_question_ids": ["GB25-BJ-Q01"]},
        },
        "aggregate": {
            "possible_points": 353,
            "questions": 74,
            "primary_metric": {"mean": 1.0, "min": 1.0, "max": 1.0},
            "earned_points": {"mean": 353, "min": 353, "max": 353},
            "correct_questions": {"mean": 74, "min": 74, "max": 74},
            "slices": {
                "text_answerable": {
                    "id": "slice.text_answerable.weighted_points",
                    "value": 1.0,
                    "numerator": 334,
                    "denominator": 334,
                    "coverage": {"selected_question_ids": ["GB25-BJ-Q01"]},
                }
            },
        },
        "primary_metric": {
            "id": "score.weighted_points",
            "value": 1.0,
            "numerator": 353,
            "denominator": 353,
            "coverage": {"selected_question_ids": ["GB25-BJ-Q01"]},
        },
        "slices": {},
        "attempts": {"total": 74, "parse_errors": 0, "truncations": 0},
        "reproducibility": {"status": "pilot_only", "caveats": ["fewer than 5 repeats"]},
        "versions": {
            "dataset": "0.0.4",
            "engine": "0.1.6",
            "harness": "0.1.6",
            "scorer": "0.1.4",
            "benchmark_spec": "1.2",
        },
        "compatibility": {"dataset": "0.0.4", "engine": "0.1.6"},
    }
    human = render_run_summary(summary)
    compact = compact_run_summary(summary)
    assert "GB25-BJ-Q01" not in human
    assert "Versions  dataset 0.0.4 · engine 0.1.6" in human
    assert "selected_question_ids" not in json.dumps(compact)
    assert compact["versions"]["scorer"] == "0.1.4"
    assert compact["scoring_policy"]["credit_policy"] == "all_or_nothing"
    assert "selected_question_ids" not in json.dumps(compact["scope"]["coverage"])


def test_subset_reporting_separates_run_scope_from_dataset_coverage():
    coverage = {
        "selected_questions": 2,
        "selected_points": 10,
        "scored_questions": 74,
        "scored_points": 353,
        "catalog_questions": 26,
        "catalog_points": 397,
        "full_paper_questions": 100,
        "full_paper_points": 750,
        "selected_question_ids": ["GB25-N1-Q01", "GB25-N1-Q02"],
    }
    summary = {
        "dataset": "Gaokao-Bench",
        "dataset_version": "0.0.4",
        "status": "completed",
        "result_status": "pilot",
        "run_id": "run-subset",
        "provider": "mock",
        "model": "mock",
        "modality": "text",
        "repeats": 1,
        "scope": {
            "coverage_label": "view:objective",
            "kind": "selected_subset",
            "selected_questions": 2,
            "selected_possible_points": 10,
            "coverage": coverage,
        },
        "aggregate": {
            "possible_points": 10,
            "questions": 2,
            "primary_metric": {"mean": 0.5, "min": 0.5, "max": 0.5},
            "earned_points": {"mean": 5, "min": 5, "max": 5},
            "correct_questions": {"mean": 1, "min": 1, "max": 1},
        },
        "slices": {},
        "attempts": {"total": 2, "parse_errors": 0, "truncations": 0},
        "reproducibility": {"status": "pilot_only", "caveats": []},
        "versions": {"dataset": "0.0.4", "engine": ENGINE_VERSION},
        "compatibility": {"dataset": "0.0.4", "engine": ENGINE_VERSION},
    }
    human = render_run_summary(summary)
    compact = compact_run_summary(summary)
    assert "Run scope  2/74 scored questions · 10/353 scored points" in human
    assert (
        "Dataset coverage  74/100 full-paper questions · 353/750 full-paper points "
        "· catalog 26 questions / 397 points"
    ) in human
    assert "GB25-N1-Q01" not in human
    assert compact["scope"]["coverage"]["selected_questions"] == 2
    assert compact["scope"]["coverage"]["scored_points"] == 353
    assert "selected_question_ids" not in json.dumps(compact)


def test_prepare_only_reporting_is_clear_and_versioned():
    summary = {
        "dataset": "Gaokao-Bench",
        "dataset_version": "0.0.4",
        "status": "prepared",
        "result_status": "prepared",
        "run_id": "run-preview",
        "provider": "openai_compatible",
        "model": "test-model",
        "modality": "vision",
        "repeats": 5,
        "selected_papers": ["GB25-N1"],
        "selected_questions": 1,
        "selected_possible_points": 5,
        "versions": {
            "dataset": "0.0.4",
            "engine": "0.1.6",
            "harness": "0.1.6",
            "scorer": "0.1.4",
            "benchmark_spec": "1.2",
        },
        "compatibility": {"dataset": "0.0.4", "engine": "0.1.6"},
        "prompt_manifest": "prompts.jsonl",
        "prompt_version": "gbmath-objective-anonymous-v2",
        "prompt_renderer_version": "1.1",
    }
    human = render_run_summary(summary, output_dir="/tmp/prompt-check")
    compact = compact_run_summary(summary)
    assert "Prepared  1 question / 5 points" in human
    assert "Prompts  1 rendered · no model calls" in human
    assert "n/a" not in human
    assert compact["status"] == "prepared"
    assert compact["result_status"] == "prepared"
    assert compact["scope"]["selected_questions"] == 1
    assert compact["versions"]["engine"] == "0.1.6"
    assert "aggregate" not in compact


def test_engine_version_is_consistent_across_release_metadata():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE).group(1) == ENGINE_VERSION
    assert re.search(r"^version: ([^\s]+)$", citation, re.MULTILINE).group(1) == ENGINE_VERSION
    assert "engine-%s-" % ENGINE_VERSION in readme
    assert "engine-%s-" % ENGINE_VERSION in readme_zh


def test_public_readmes_and_theme_assets_are_complete():
    readme = ROOT / "README.md"
    readme_zh = ROOT / "README.zh-CN.md"
    assert readme.is_file()
    assert readme_zh.is_file()
    assert "README.zh-CN.md" in readme.read_text(encoding="utf-8")
    assert "README.md" in readme_zh.read_text(encoding="utf-8")

    expected_assets = {
        "logo-light.svg",
        "logo-dark.svg",
        "hero-light.svg",
        "hero-dark.svg",
        "hero-zh-light.svg",
        "hero-zh-dark.svg",
        "hero-hf-zh.svg",
        "q11-editorial-light.svg",
        "q11-editorial-dark.svg",
        "pipeline-light.svg",
        "pipeline-dark.svg",
        "pipeline-zh-light.svg",
        "pipeline-zh-dark.svg",
    }
    asset_dir = ROOT / ".github" / "readme"
    assert {path.name for path in asset_dir.glob("*.svg")} == expected_assets
    assert (asset_dir / "hero-hf-zh.svg").read_bytes() == (asset_dir / "hero-zh-light.svg").read_bytes()

    for document in (readme, readme_zh):
        text = document.read_text(encoding="utf-8")
        for target in re.findall(r'(?:href|src|srcset)="([^"]+)"', text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            assert (ROOT / target).is_file(), "%s has a missing local reference: %s" % (document.name, target)

    for path in asset_dir.glob("pipeline-*.svg"):
        text = path.read_text(encoding="utf-8")
        assert "two independent reviewers" not in text
        assert "两名独立审校" not in text


def test_public_fetch_data_cli_contract():
    args = build_parser().parse_args(["fetch-data", "--version", "0.0.1"])
    assert args.command == "fetch-data"
    assert args.repo_id == "Knove/Gaokao-Bench"
    assert args.data_dir == "data"
    assert args.version == "0.0.1"
    assert args.revision is None


def test_public_package_contract_is_documented():
    assert "README.md" in PUBLIC_REQUIRED_FILES
    assert "manifest.json" not in PUBLIC_REQUIRED_FILES
    for document in (ROOT / "README.md", ROOT / "README.zh-CN.md"):
        text = document.read_text(encoding="utf-8")
        assert "https://huggingface.co/datasets/Knove/Gaokao-Bench" in text
        assert "gaokao-bench fetch-data --version 0.0.4" in text
        assert "https://github.com/Knove/Gaokao-Bench.git" in text
        assert "--allow-fallbacks false" not in text
        assert 'python -m pip install -e ".[harness,data]"' in text
        assert "pytest -m \"not public_data and not source_release\"" in text


def test_huggingface_dataset_card_metadata_is_hub_compatible():
    card = _dataset_card(
        repo_id="Knove/Gaokao-Bench",
        dataset_version="0.0.4",
        audit={
            "questions": 74,
            "objective_points": 353,
            "text_points": 334,
            "answer_slots": 78,
            "image_required_questions": 4,
            "catalog_subjective_questions": 26,
            "catalog_subjective_points": 397,
            "created_at": "2026-11-10T00:00:00+00:00",
        },
        benchmark_spec={"path": "benchmark_spec.json"},
    )
    front_matter = card.split("---", 2)[1]
    assert re.search(r"^license: cc-by-4\.0$", front_matter, re.MULTILINE)
    assert "license_name:" not in front_matter
    brand_url = "https://huggingface.co/datasets/Knove/Gaokao-Bench/resolve/v0.0.4/brand"
    assert f"{brand_url}/hero-hf-zh.png" in card
    assert f"{brand_url}/q11-editorial-light.png" in card
    assert f"{brand_url}/q11-editorial-dark.png" in card
    assert 'srcset="brand/q11-editorial-dark.png"' not in card
    configs = re.findall(r"^- config_name: (.+)$", front_matter, re.MULTILINE)
    assert configs == ["questions"]
    assert "`benchmark_spec.json`" in card
    assert "公开榜单" in card
    assert "厂商宣传" in card
    assert "无需额外授权" in card
    assert "public_ranking_allowed" not in card
    assert "整个数据包" in card
    assert "NOASSERTION" not in card


def test_dataset_card_has_no_ranking_authorization_gate():
    card = _dataset_card(
        repo_id="Knove/Gaokao-Bench",
        dataset_version="0.0.0-test",
        audit={
            "questions": 1,
            "objective_points": 5,
            "text_points": 5,
            "answer_slots": 1,
            "image_required_questions": 0,
            "catalog_subjective_questions": 0,
            "catalog_subjective_points": 0,
            "created_at": "2026-11-11T00:00:00+00:00",
        },
    )
    assert "公开榜单" in card
    assert "无需额外授权" in card
    assert "public_ranking_allowed" not in card
    assert "不应把本数据包的结果用于第三方公开榜单" not in card


def test_huggingface_dataset_card_is_spec_driven_for_other_subjects():
    spec = parse_benchmark_spec(
        {
            "schema_version": "1.0",
            "id": "gaokao-physics-2024-2025",
            "version": "1.0",
            "subject": "物理",
            "years": [2024, 2025],
            "task_types": {"single_choice": {"shape": "choice", "min_selections": 1, "max_selections": 1}},
            "choice_labels": ["A", "B", "C", "D"],
            "prompt": {"version": "physics-v1", "system": "只输出 JSON"},
            "scoring": {"profile": "deterministic", "metric_version": "1.0"},
            "expected": {"question_count": 2, "paper_count": 1, "total_points": 10},
        }
    )
    card = _dataset_card(
        repo_id="Knove/Gaokao-Bench",
        dataset_version="0.0.3",
        audit={
            "papers": 1,
            "questions": 2,
            "objective_points": 10,
            "text_points": 10,
            "answer_slots": 0,
            "image_required_questions": 0,
            "catalog_subjective_questions": 0,
            "catalog_subjective_points": 0,
            "created_at": "2026-11-10T00:00:00+00:00",
        },
        spec=spec,
        papers=[{"id": "GB24-PH", "title": "2024 物理卷", "subject": "物理", "year": 2024}],
        benchmark_spec={"path": "benchmark_spec.json"},
        score_label="retrospective_public_2024-2025",
    )
    assert "物理" in card
    assert "2024" in card
    assert "physics" in card
    assert "retrospective_public_2024-2025" in card
    assert "2025 高考数学" not in card
    assert "`benchmark_spec.json`" in card
    assert "- - " not in card
