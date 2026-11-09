#!/usr/bin/env python3
"""Export two blank, independent review assignments."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.review import export_review_packet  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.1")
    parser.add_argument("--out", help="defaults to review_packets/<dataset-version>-independent")
    parser.add_argument("--created-at", help="optional deterministic ISO-8601 timestamp")
    parser.add_argument("--force", action="store_true", help="replace a non-empty output directory")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    version = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))["dataset_version"]
    output = args.out or "review_packets/%s-independent" % version
    manifest = export_review_packet(
        data_dir,
        output,
        created_at=args.created_at,
        force=args.force,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
