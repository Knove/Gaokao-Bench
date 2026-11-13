#!/usr/bin/env python3
"""Deprecated shim for the one version-independent acceptance dashboard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.acceptance_dashboard import DEFAULT_NOTES_PATH, build_acceptance_page  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.1.0-alpha.2026-n1.1")
    parser.add_argument("--review-root", default="review_packets")
    parser.add_argument("--output", default="reports/acceptance/index.html")
    parser.add_argument("--notes", default=str(DEFAULT_NOTES_PATH))
    parser.add_argument("--only-version", action="store_true", help="restrict the archive to this data package")
    args = parser.parse_args()

    data_dir = (ROOT / args.data_dir).resolve() if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest["dataset_version"])
    result = build_acceptance_page(
        data_root=data_dir.parent,
        review_root=(ROOT / args.review_root).resolve(),
        output=(ROOT / args.output).resolve(),
        versions=[version] if args.only_version else None,
        notes_path=(ROOT / args.notes).resolve() if args.notes else None,
        default_version=version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
