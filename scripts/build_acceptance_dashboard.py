#!/usr/bin/env python3
"""Build the single owner acceptance dashboard for every local data version."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.acceptance_dashboard import (  # noqa: E402
    DEFAULT_NOTES_PATH,
    DEFAULT_UI_DIR,
    build_acceptance_page,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data", help="directory containing data/<version>/ packages")
    parser.add_argument("--review-root", default="review_packets", help="directory containing owner review packets")
    parser.add_argument(
        "--output",
        default="reports/acceptance/index.html",
        help="canonical, version-independent acceptance page",
    )
    parser.add_argument(
        "--version",
        action="append",
        dest="versions",
        help="limit the build to a dataset version; repeat for multiple versions",
    )
    parser.add_argument("--default-version", help="version selected when no deep link is present")
    parser.add_argument("--notes", default=str(DEFAULT_NOTES_PATH), help="curated priority notes JSON")
    parser.add_argument("--ui-dir", default=str(DEFAULT_UI_DIR), help="dashboard template directory")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="open the generated file:// page")
    parser.add_argument("--quiet", action="store_true", help="suppress the JSON build summary")
    args = parser.parse_args()

    try:
        result = build_acceptance_page(
            data_root=(ROOT / args.data_root).resolve(),
            review_root=(ROOT / args.review_root).resolve(),
            ui_dir=(ROOT / args.ui_dir).resolve() if not Path(args.ui_dir).is_absolute() else Path(args.ui_dir),
            output=(ROOT / args.output).resolve(),
            versions=args.versions,
            notes_path=(ROOT / args.notes).resolve() if args.notes else None,
            default_version=args.default_version,
        )
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if args.open_browser:
        webbrowser.open(Path(result["output"]).as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
