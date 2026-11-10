#!/usr/bin/env python3
"""Validate two signed independent reviews and emit proposals, never edits."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.review import import_review_packet, write_review_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.2")
    parser.add_argument("--answers-1", required=True)
    parser.add_argument("--answers-2", required=True)
    parser.add_argument("--points-1", required=True)
    parser.add_argument("--points-2", required=True)
    parser.add_argument("--arbitration", help="JSON file for conflicts reported by a first import")
    parser.add_argument("--output", help="defaults to reports/<dataset-version>-independent-review-import.json")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    version = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))["dataset_version"]
    output = args.output or "reports/%s-independent-review-import.json" % version
    report = import_review_packet(
        data_dir,
        answers_1=args.answers_1,
        answers_2=args.answers_2,
        points_1=args.points_1,
        points_2=args.points_2,
        arbitration=args.arbitration,
    )
    write_review_report(report, output)
    summary = {
        "dataset_version": report["dataset_version"],
        "status": report["status"],
        "human_independent_review": report["human_independent_review"],
        "dataset_mutated": report["dataset_mutated"],
        "counts": report["counts"],
        "errors": report["errors"],
        "blocking_items": report["blocking_items"],
        "proposals": report["proposals"],
        "output": str(Path(output).resolve()),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] == "invalid":
        return 2
    if report["status"] != "complete":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
