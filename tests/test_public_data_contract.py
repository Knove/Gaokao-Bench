import json
import os
from pathlib import Path

import pytest

from gaokao_bench.cli import main
from gaokao_bench.harness import (
    FixedResponseProvider,
    build_answer_instruction,
    build_prompt,
    render_system_prompt,
    run_benchmark,
)
from gaokao_bench.public_data import build_public_data_package
from gaokao_bench.scoring import (
    PUBLIC_PACKAGE_PROFILE,
    evaluate_submission,
    load_dataset,
    score_question,
    validate_dataset,
)
from gaokao_bench.version import DEFAULT_DATASET_VERSION


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(
    os.environ.get("GAOKAO_BENCH_DATA_DIR", ROOT / "data" / DEFAULT_DATASET_VERSION)
)


def _public_package_ready() -> bool:
    if not (DATA / "manifest.json").is_file():
        return False
    try:
        manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return manifest.get("package_profile") == PUBLIC_PACKAGE_PROFILE


pytestmark = [
    pytest.mark.public_data,
    pytest.mark.skipif(
        not _public_package_ready(),
        reason="public data package not present; run `gaokao-bench fetch-data` first",
    ),
]


def test_public_package_has_public_layout_and_valid_audit():
    dataset = load_dataset(DATA, validate=False)
    assert dataset.manifest["package_profile"] == PUBLIC_PACKAGE_PROFILE
    assert dataset.manifest["dataset_version"] == DEFAULT_DATASET_VERSION
    assert not (DATA / "answer_audit.jsonl").exists()
    assert (DATA / "audit" / "answer_audit.jsonl").is_file()
    assert (DATA / "audit" / "owner_signoff.json").is_file()
    audit = validate_dataset(dataset)
    assert audit["questions"] == 74
    assert audit["objective_points"] == 353


def test_public_gold_submission_scores_the_full_denominator():
    dataset = load_dataset(DATA)
    gold = json.loads((DATA / "examples" / "gold.json").read_text(encoding="utf-8"))
    report = evaluate_submission(dataset, gold["answers"])
    assert report["summary"]["earned_points"] == 353
    assert report["summary"]["possible_points"] == 353
    assert report["primary_metric"]["value"] == 1.0


def test_beijing_q15_prompt_and_scorer_accept_the_same_set_semantics():
    dataset = load_dataset(DATA)
    question = next(item for item in dataset.questions if item["id"] == "GB25-BJ-Q15")
    key = dataset.answer_keys["GB25-BJ-Q15"]
    prompt = build_prompt(question)
    assert "序号填空题" in prompt
    assert "不要拆成数组" in prompt
    string_result = score_question(question, key, "②③", True)
    list_result = score_question(question, key, ["②", "③"], True)
    assert string_result["status"] == "correct"
    assert list_result["status"] == "correct"


def test_beijing_q13_accepts_any_valid_trig_pair():
    dataset = load_dataset(DATA)
    question = next(item for item in dataset.questions if item["id"] == "GB25-BJ-Q13")
    key = dataset.answer_keys["GB25-BJ-Q13"]
    for answer in (["π/2", "π/4"], ["π/2", "π/6"], ["3π/2", "π/3"]):
        assert score_question(question, key, answer, True)["status"] == "correct"
    for answer in (["π/2", "π"], ["0", "π/4"]):
        assert score_question(question, key, answer, True)["status"] == "incorrect"


def test_prompt_manifest_records_the_answer_format_contract(tmp_path):
    dataset = load_dataset(DATA)
    manifest = run_benchmark(
        dataset,
        FixedResponseProvider('{"answer":"A"}'),
        provider_name="mock",
        model="mock-model",
        modality="text",
        repeats=1,
        output_dir=tmp_path / "prepare",
        paper_ids=["GB25-BJ"],
        prepare_only=True,
        created_at="2026-11-11T00:00:00+08:00",
    )
    assert manifest["status"] == "prepared"
    assert manifest["result_status"] == "prepared"
    assert manifest["versions"]["dataset"] == "0.0.4"
    assert manifest["versions"]["engine"] == "0.1.5"
    rows = [
        json.loads(line)
        for line in (tmp_path / "prepare" / "prompts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    question = next(row for row in rows if row["question_id"] == "GB25-BJ-Q15")
    assert question["answer_format"] == "circled_set"
    assert "序号填空题" in question["answer_instruction"]
    assert "②③" not in question["answer_instruction"]
    assert question["answer_instruction_sha256"]


def test_public_vision_prompts_materialize_all_reviewed_assets(tmp_path):
    dataset = load_dataset(DATA)
    run_benchmark(
        dataset,
        FixedResponseProvider('{"answer":"A"}'),
        provider_name="mock",
        model="mock-model",
        modality="vision",
        repeats=1,
        output_dir=tmp_path / "vision-prepare",
        prepare_only=True,
        created_at="2026-11-11T00:00:00+08:00",
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "vision-prepare" / "prompts.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    expected = {
        question["id"]
        for question in dataset.questions
        if question["asset_dependency"] == "required"
    }
    materialized = {row["question_id"] for row in rows if row.get("image")}
    assert expected == materialized
    assert len(materialized) == 4
    assert all(row["image"]["pixel_sha256"] for row in rows if row.get("image"))


def test_public_system_prompt_examples_cannot_leak_answers():
    dataset = load_dataset(DATA)
    rendered = render_system_prompt(dataset.spec.prompt.system)
    assert '"C"' not in rendered
    assert '"ACD"' not in rendered
    assert '"<单个选项字母>"' in rendered
    assert '"<选项字母串>"' in rendered
    for key in dataset.answer_keys.values():
        canonical = key.get("canonical")
        if isinstance(canonical, str) and len(canonical) >= 2:
            assert json.dumps(canonical, ensure_ascii=False) not in rendered


def test_prompt_format_examples_never_contain_an_actual_answer():
    dataset = load_dataset(DATA)
    for question in dataset.questions:
        canonical = dataset.answer_keys[question["id"]].get("canonical")
        if not isinstance(canonical, str) or len(canonical) < 2:
            continue
        instruction = build_answer_instruction(question)
        assert json.dumps(canonical, ensure_ascii=False) not in instruction


def test_public_builder_rejects_a_public_package_as_source(tmp_path):
    output = tmp_path / DEFAULT_DATASET_VERSION
    with pytest.raises(ValueError, match="public-data packages cannot be rebuilt"):
        build_public_data_package(DATA, output)
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_evaluate_cli_prints_a_short_table_and_writes_full_json(tmp_path, capsys):
    report_path = tmp_path / "report.json"
    code = main(
        [
            "evaluate",
            "--data-dir",
            str(DATA),
            "--answers",
            str(DATA / "examples" / "gold.json"),
            "--report",
            str(report_path),
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "Primary" in output
    assert "353 / 353 points" in output
    assert "GB25-BJ-Q01" not in output
    assert len(output.splitlines()) < 30
    full_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert full_report["summary"]["earned_points"] == 353
    assert full_report["primary_metric"]["coverage"]["selected_question_ids"]


def test_evaluate_cli_json_and_quiet_modes(tmp_path, capsys):
    assert (
        main(
            [
                "evaluate",
                "--data-dir",
                str(DATA),
                "--answers",
                str(DATA / "examples" / "gold.json"),
                "--json",
            ]
        )
        == 0
    )
    compact = json.loads(capsys.readouterr().out)
    assert "selected_question_ids" not in json.dumps(compact)
    assert compact["primary_metric"]["value"] == 1.0

    assert (
        main(
            [
                "evaluate",
                "--data-dir",
                str(DATA),
                "--answers",
                str(DATA / "examples" / "gold.json"),
                "--quiet",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""


def test_evaluate_cli_errors_are_actionable(tmp_path, capsys):
    missing_data = tmp_path / "missing-data"
    assert (
        main(
            [
                "evaluate",
                "--data-dir",
                str(missing_data),
                "--answers",
                str(DATA / "examples" / "gold.json"),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "data package not found" in error
    assert "fetch-data --version" in error
    assert "Traceback" not in error

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{broken", encoding="utf-8")
    assert (
        main(
            [
                "evaluate",
                "--data-dir",
                str(DATA),
                "--answers",
                str(malformed),
            ]
        )
        == 2
    )
    error = capsys.readouterr().err
    assert "answers file is not valid JSON" in error
    assert "Traceback" not in error

    wrong_shape = tmp_path / "wrong-shape.json"
    wrong_shape.write_text('{"answers": []}', encoding="utf-8")
    assert (
        main(
            [
                "evaluate",
                "--data-dir",
                str(DATA),
                "--answers",
                str(wrong_shape),
            ]
        )
        == 2
    )
    assert "submission.answers must be an object" in capsys.readouterr().err
