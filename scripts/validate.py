#!/usr/bin/env python3
"""Validate the checked-in Gaokao-Bench alpha package."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gaokao_bench.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["validate"] + sys.argv[1:]))

