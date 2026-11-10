#!/usr/bin/env python3
"""Build a sanitized Hugging Face data package from the internal corpus."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.public_data import build_public_data_package  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="data/0.0.2")
    parser.add_argument("--output-dir", default="dist/public-data/0.0.2")
    parser.add_argument("--repo-id", default="Knove/Gaokao-Bench")
    parser.add_argument(
        "--created-at",
        help="ISO 8601 UTC timestamp; fix this value for reproducible release builds",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing output directory")
    args = parser.parse_args()

    source = (ROOT / args.source_dir).resolve()
    output = (ROOT / args.output_dir).resolve()
    if output.exists():
        if not args.force:
            print(
                "output already exists: %s (use --force to replace it)" % output,
                file=sys.stderr,
            )
            return 2
        shutil.rmtree(output)

    try:
        result = build_public_data_package(
            source,
            output,
            repo_id=args.repo_id,
            created_at=args.created_at,
        )
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

