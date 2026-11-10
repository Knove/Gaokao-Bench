#!/usr/bin/env python3
"""Run the deterministic alpha smoke test and write a reproducible report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.scoring import (  # noqa: E402
    SCORER_VERSION,
    evaluate_submission,
    file_sha256,
    load_dataset,
    validate_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.1")
    parser.add_argument("--output", help="defaults to reports/<dataset-version>-smoke.json")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset_version = str(manifest["dataset_version"])
    dataset = load_dataset(data_dir)
    audit = validate_dataset(dataset)
    possible_points = sum(int(question["points"]) for question in dataset.questions)
    cases = {}
    for name, expected_points in (("gold", possible_points), ("empty", 0), ("malformed", 0)):
        submission = json.loads((data_dir / "examples" / (name + ".json")).read_text(encoding="utf-8"))
        report = evaluate_submission(dataset, submission["answers"])
        assert report["summary"]["earned_points"] == expected_points, name
        assert report["summary"]["possible_points"] == possible_points, name
        cases[name] = {
            "expected_points": expected_points,
            "earned_points": report["summary"]["earned_points"],
            "primary_metric": report["primary_metric"],
            "failure_counts": report["summary"]["failure_counts"],
        }
    payload = {
        "dataset": "Gaokao-Bench",
        "dataset_version": dataset_version,
        "dataset_manifest_sha256": file_sha256(data_dir / "manifest.json"),
        "scorer_version": SCORER_VERSION,
        "audit": audit,
        "smoke_cases": cases,
        "status": "pass",
    }
    output = Path(args.output) if args.output else ROOT / "reports" / ("%s-smoke.json" % dataset_version)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

