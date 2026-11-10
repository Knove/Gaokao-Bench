#!/usr/bin/env python3
"""Materialise completed signatures into a new immutable dataset version."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.materialize import (  # noqa: E402
    materialize_signed_dataset,
    write_materialized_release_evidence,
    write_materialization_report,
    write_smoke_report,
)
from gaokao_bench.version import DEFAULT_DATASET_VERSION  # noqa: E402


def _new_file(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise ValueError("report already exists; pass --force to replace derived reports: %s" % path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/%s" % DEFAULT_DATASET_VERSION)
    parser.add_argument("--output-dir", required=True, help="new directory, whose basename equals --version")
    parser.add_argument("--version", required=True, help="new immutable dataset version")
    parser.add_argument("--created-at", help="ISO-8601 timestamp with timezone")
    parser.add_argument("--owner-review-csv", required=True)
    parser.add_argument("--owner-signoff", required=True)
    parser.add_argument("--answers-1", required=True)
    parser.add_argument("--answers-2", required=True)
    parser.add_argument("--points-1", required=True)
    parser.add_argument("--points-2", required=True)
    parser.add_argument("--arbitration", help="third-party arbitration JSON when imports reported conflicts")
    parser.add_argument("--reports-dir", default="reports")
    parser.add_argument("--release-root", default="release")
    parser.add_argument("--force", action="store_true", help="replace only the derived smoke/materialisation reports")
    args = parser.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.is_absolute():
        reports_dir = ROOT / reports_dir
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    release_root = Path(args.release_root)
    if not release_root.is_absolute():
        release_root = ROOT / release_root
    materialization_report = reports_dir / ("%s-materialization.json" % args.version)
    smoke_report = reports_dir / ("%s-smoke.json" % args.version)
    release_evidence = release_root / args.version
    result = None
    release_evidence_created = False
    report_backups = {
        materialization_report: materialization_report.read_bytes() if materialization_report.is_file() else None,
        smoke_report: smoke_report.read_bytes() if smoke_report.is_file() else None,
    }
    try:
        _new_file(materialization_report, args.force)
        _new_file(smoke_report, args.force)
        if release_evidence.exists():
            raise ValueError("release evidence directory already exists: %s" % release_evidence)
        source_version = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))["dataset_version"]
        source_release = release_root / str(source_version)
        if not source_release.is_dir():
            raise ValueError("source release evidence directory is missing: %s" % source_release)
        result = materialize_signed_dataset(
            data_dir,
            args.output_dir,
            target_version=args.version,
            created_at=args.created_at,
            owner_review_csv=args.owner_review_csv,
            owner_signoff=args.owner_signoff,
            answers_1=args.answers_1,
            answers_2=args.answers_2,
            points_1=args.points_1,
            points_2=args.points_2,
            arbitration=args.arbitration,
        )
        release_files = write_materialized_release_evidence(
            data_dir,
            result["output_dir"],
            result,
            release_root=release_root,
        )
        release_evidence_created = True
        write_materialization_report(result, materialization_report)
        write_smoke_report(result, smoke_report)
    except Exception as exc:
        if result is not None:
            shutil.rmtree(result["output_dir"], ignore_errors=True)
        if release_evidence_created:
            shutil.rmtree(release_evidence, ignore_errors=True)
        for path, previous in report_backups.items():
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(previous)
            except OSError:
                pass
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    summary = {
        "status": result["status"],
        "source_version": result["source_version"],
        "target_version": result["target_version"],
        "output_dir": result["output_dir"],
        "output_manifest_sha256": result["output_manifest_sha256"],
        "review_events": result["review_events"],
        "gold": result["gold"],
        "materialization_report": str(materialization_report.resolve()),
        "smoke_report": str(smoke_report.resolve()),
        "release_evidence": release_files,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
