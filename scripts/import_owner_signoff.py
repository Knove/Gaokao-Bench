#!/usr/bin/env python3
"""Validate one owner signoff and emit proposals, never dataset edits."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.owner_review import import_owner_signoff, write_owner_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.3")
    parser.add_argument("--review-csv", required=True)
    parser.add_argument("--signoff", required=True)
    parser.add_argument("--output", help="defaults to reports/<dataset-version>-owner-signoff.json")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    version = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))["dataset_version"]
    output = args.output or "reports/%s-owner-signoff.json" % version
    report = import_owner_signoff(
        data_dir,
        signoff=args.signoff,
        review_csv=args.review_csv,
    )
    write_owner_report(report, output)
    summary = {
        "dataset_version": report["dataset_version"],
        "status": report["status"],
        "owner_signoff": report["owner_signoff"],
        "human_independent_review": report["human_independent_review"],
        "dataset_mutated": report["dataset_mutated"],
        "counts": report["counts"],
        "errors": report["errors"],
        "disputes": report["disputes"],
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
