#!/usr/bin/env python3
"""Verify the file manifest, checksum, and content scope of a release ZIP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.release_bundle import ReleaseBundleError, verify_release_bundle  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--sidecar")
    args = parser.parse_args()
    try:
        result = verify_release_bundle(args.bundle, sidecar=args.sidecar)
    except ReleaseBundleError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
