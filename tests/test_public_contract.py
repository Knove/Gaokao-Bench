import re
from pathlib import Path

from gaokao_bench.cli import build_parser
from gaokao_bench.public_data import _dataset_card
from gaokao_bench.scoring import parse_interval, parse_numeric_expression
from gaokao_bench.scoring import PUBLIC_REQUIRED_FILES


ROOT = Path(__file__).resolve().parents[1]


def test_public_numeric_parser_contract():
    assert parse_numeric_expression("7/4") == 1.75
    assert parse_numeric_expression(r"\frac{7}{4}") == 1.75
    assert abs(parse_numeric_expression("3√2") - 4.242640687119286) < 1e-12


def test_public_interval_parser_contract():
    assert parse_interval("[4,5]") == (4.0, 5.0, True, True)
    assert parse_interval("(1,3)") == (1.0, 3.0, False, False)
    assert parse_interval("{x | 1 < x <= 3}") == (1.0, 3.0, False, True)


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
        assert "gaokao-bench fetch-data --version 0.0.1" in text


def test_huggingface_dataset_card_metadata_is_hub_compatible():
    card = _dataset_card(
        repo_id="Knove/Gaokao-Bench",
        dataset_version="0.0.1",
        audit={
            "questions": 74,
            "objective_points": 353,
            "text_points": 334,
            "answer_slots": 78,
            "image_required_questions": 4,
            "catalog_subjective_questions": 26,
            "catalog_subjective_points": 397,
            "created_at": "2026-11-09T00:00:00+00:00",
        },
    )
    front_matter = card.split("---", 2)[1]
    license_name = re.search(r"^license_name: (.+)$", front_matter, re.MULTILINE)
    assert license_name is not None
    assert re.fullmatch(r"[a-z0-9.-]+", license_name.group(1))
    assert "brand/hero-hf-zh.png" in card
    configs = re.findall(r"^- config_name: (.+)$", front_matter, re.MULTILINE)
    assert configs == ["questions"]
