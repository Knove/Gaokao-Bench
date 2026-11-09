#!/usr/bin/env python3
"""Run the Gaokao-Bench scorer from a checkout without installing the package."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gaokao_bench.cli import main  # noqa: E402


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0] not in ("evaluate", "validate"):
        arguments = ["evaluate"] + arguments
    raise SystemExit(main(arguments))

