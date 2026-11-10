#!/usr/bin/env python3
"""Build a reproducible Gaokao-Bench release bundle after the release gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.release import resolve_release_report_paths  # noqa: E402
from gaokao_bench.release_bundle import ReleaseBundleError, build_release_bundle  # noqa: E402
from gaokao_bench.version import DEFAULT_DATASET_VERSION  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("internal-candidate", "public-code", "public-candidate"), required=True)
    parser.add_argument("--data-dir", default="data/%s" % DEFAULT_DATASET_VERSION)
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--allow-blocked", action="store_true", help="build a clearly marked blocked diagnostic bundle")
    parser.add_argument("--owner-report", help="defaults from the dataset's review-materialisation lineage")
    parser.add_argument("--independent-report", help="defaults from the dataset's review-materialisation lineage")
    parser.add_argument("--smoke-report", help="defaults to reports/<current-dataset-version>-smoke.json")
    parser.add_argument("--run-summary")
    parser.add_argument("--release-policy")
    parser.add_argument("--rights-attestation")
    parser.add_argument("--answer-protocol")
    parser.add_argument("--created-at", help="deterministic ISO-8601 timestamp for reproducible manifests")
    args = parser.parse_args()

    try:
        data_dir = Path(args.data_dir)
        if not data_dir.is_absolute():
            data_dir = ROOT / data_dir
        report_paths = resolve_release_report_paths(data_dir, repo_root=ROOT)
        result = build_release_bundle(
            ROOT,
            profile=args.profile,
            data_dir=data_dir,
            output_dir=args.output_dir,
            allow_blocked=args.allow_blocked,
            created_at=args.created_at,
            gate_kwargs={
                "owner_report": args.owner_report or str(report_paths["owner_report"]),
                "independent_report": args.independent_report or str(report_paths["independent_report"]),
                "smoke_report": args.smoke_report or str(report_paths["smoke_report"]),
                "run_summary": args.run_summary,
                "release_policy": args.release_policy,
                "rights_attestation": args.rights_attestation,
                "answer_protocol": args.answer_protocol,
            },
        )
    except (ReleaseBundleError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["release_status"] == "pass" or args.allow_blocked else 1


if __name__ == "__main__":
    raise SystemExit(main())
