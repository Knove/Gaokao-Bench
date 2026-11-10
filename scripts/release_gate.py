#!/usr/bin/env python3
"""Check whether Gaokao-Bench has enough evidence for a release target."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.release import evaluate_release_gate, write_release_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.2")
    parser.add_argument("--target", choices=("internal", "public-code", "public"), default="internal")
    parser.add_argument("--owner-report", help="defaults to reports/<dataset-version>-owner-signoff.json")
    parser.add_argument("--independent-report", help="defaults to reports/<dataset-version>-independent-review-import.json")
    parser.add_argument("--smoke-report", help="defaults to reports/<dataset-version>-smoke.json")
    parser.add_argument("--run-summary", help="vision run summary.json for a clean release run")
    parser.add_argument("--release-policy", help="release-policy.json bound to the dataset manifest")
    parser.add_argument("--rights-attestation", help="signed rights attestation for a full public dataset")
    parser.add_argument("--answer-protocol", help="signed alternative answer-validation protocol")
    parser.add_argument("--output", help="defaults to reports/release-gates/<dataset-version>-<target>.json")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset_version = str(manifest["dataset_version"])
    owner_report = args.owner_report or str(ROOT / "reports" / ("%s-owner-signoff.json" % dataset_version))
    independent_report = args.independent_report or str(
        ROOT / "reports" / ("%s-independent-review-import.json" % dataset_version)
    )
    smoke_report = args.smoke_report or str(ROOT / "reports" / ("%s-smoke.json" % dataset_version))
    output_path = args.output or str(
        ROOT / "reports" / "release-gates" / ("%s-%s.json" % (dataset_version, args.target))
    )

    report = evaluate_release_gate(
        data_dir,
        target=args.target,
        owner_report=owner_report,
        independent_report=independent_report,
        smoke_report=smoke_report,
        run_summary=args.run_summary,
        release_policy=args.release_policy,
        rights_attestation=args.rights_attestation,
        answer_protocol=args.answer_protocol,
        repo_root=ROOT,
    )
    output = write_release_report(report, output_path)
    print(
        json.dumps(
            {
                "target": report["target"],
                "status": report["status"],
                "dataset_version": report["dataset_version"],
                "harness_version": report["harness_version"],
                "scorer_version": report["scorer_version"],
                "blocking_checks": report["blocking_checks"],
                "warnings": report["warnings"],
                "output": str(output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

