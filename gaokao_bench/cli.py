"""Command line entry points for validation and scoring."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List, Optional

from .data_fetch import (
    DEFAULT_DATA_REPO_ID,
    DEFAULT_DATA_VERSION,
    DataFetchError,
    fetch_data,
)
from .reporting import compact_evaluation_summary, render_evaluation_summary
from .scoring import evaluate_submission, load_dataset, validate_dataset
from .version import DEFAULT_DATASET_VERSION


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_cli_dataset(data_dir: str) -> Any:
    root = Path(data_dir)
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise ValueError(
            "data package not found at %s; run `gaokao-bench fetch-data --version %s` first"
            % (root.resolve(), DEFAULT_DATASET_VERSION)
        )
    return load_dataset(root, validate=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gaokao-Bench utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch-data", help="download and verify a public dataset version from Hugging Face"
    )
    fetch_parser.add_argument("--version", default=DEFAULT_DATA_VERSION)
    fetch_parser.add_argument("--repo-id", default=DEFAULT_DATA_REPO_ID)
    fetch_parser.add_argument(
        "--data-dir",
        default="data",
        help="base data directory; the version is created beneath it",
    )
    fetch_parser.add_argument(
        "--revision",
        help="Hugging Face revision or tag (default: v<version>)",
    )
    fetch_parser.add_argument(
        "--force", action="store_true", help="replace an existing directory after verification"
    )

    validate_parser = subparsers.add_parser("validate", help="validate the immutable data package")
    validate_parser.add_argument("--data-dir", default="data/%s" % DEFAULT_DATASET_VERSION)

    evaluate_parser = subparsers.add_parser("evaluate", help="score a submission JSON file")
    evaluate_parser.add_argument("--data-dir", default="data/%s" % DEFAULT_DATASET_VERSION)
    evaluate_parser.add_argument("--answers", required=True, help="JSON file containing {'answers': {...}}")
    evaluate_parser.add_argument(
        "--report",
        "--output",
        dest="report",
        help="write the complete JSON report to this path",
    )
    output_mode = evaluate_parser.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--json",
        "--summary-only",
        dest="json",
        action="store_true",
        help="print a compact machine-readable summary instead of the table",
    )
    output_mode.add_argument("--quiet", action="store_true", help="suppress the result summary")
    evaluate_parser.add_argument("--strict", action="store_true", help="fail on unknown question IDs")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "fetch-data":
        try:
            result = fetch_data(
                version=args.version,
                data_dir=args.data_dir,
                repo_id=args.repo_id,
                revision=args.revision,
                force=args.force,
            )
        except DataFetchError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        _print_json(result)
        return 0

    try:
        dataset = _load_cli_dataset(args.data_dir)
        audit = validate_dataset(dataset)

        if args.command == "validate":
            _print_json({"status": "ok", "audit": audit})
            return 0

        submission_path = Path(args.answers)
        try:
            submission = json.loads(submission_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("answers file not found: %s" % submission_path) from exc
        except json.JSONDecodeError as exc:
            raise ValueError("answers file is not valid JSON: %s: %s" % (submission_path, exc)) from exc
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read answers file %s: %s" % (submission_path, exc)) from exc
        if not isinstance(submission, dict) or "answers" not in submission:
            raise ValueError("submission must be an object with an 'answers' field")
        if not isinstance(submission["answers"], dict):
            raise ValueError("submission.answers must be an object keyed by question ID")

        report = evaluate_submission(dataset, submission["answers"])
        report["submission_path"] = str(submission_path)
        report["data_dir"] = str(Path(args.data_dir).resolve())
        if args.report:
            output_path = Path(args.report)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if not args.quiet:
            if args.json:
                _print_json(compact_evaluation_summary(report))
            else:
                print(render_evaluation_summary(report))
                if args.report:
                    print("Report  %s" % Path(args.report).resolve())
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.strict and report["unexpected_answer_ids"]:
        print("strict mode: unknown answer IDs were supplied", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

