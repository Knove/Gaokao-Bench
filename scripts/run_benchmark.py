#!/usr/bin/env python3
"""Run a text or required-vision Gaokao-Bench model evaluation."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.harness import (  # noqa: E402
    FixedResponseProvider,
    build_provider,
    run_benchmark,
)
from gaokao_bench.scoring import load_dataset  # noqa: E402


def _default_output_dir(provider: str, modality: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "runs" / ("%s-%s-%s" % (stamp, provider, modality))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data/0.0.1")
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openai_compatible", "gemini_cli", "mock"),
        required=True,
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--modality", choices=("text", "vision"), default="text")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--papers", help="comma-separated paper IDs, e.g. GB25-N1,GB25-BJ")
    parser.add_argument("--limit", type=int, help="first N selected questions, for a pilot only")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens-field", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    parser.add_argument("--api-key-env")
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir")
    parser.add_argument("--report", help="optional path for a copy of summary.json")
    parser.add_argument("--created-at", help="deterministic ISO-8601 timestamp for tests/replays")
    parser.add_argument("--prepare-only", action="store_true", help="build and audit prompts without model calls")
    parser.add_argument("--mock-response", default='{"answer": "A"}')
    args = parser.parse_args()

    paper_ids = None
    if args.papers:
        paper_ids = [item.strip() for item in args.papers.split(",") if item.strip()]
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(args.provider, args.modality)
    dataset = load_dataset(args.data_dir)

    if args.prepare_only:
        provider = FixedResponseProvider(args.mock_response)
        provider_metadata = {
            "provider": args.provider,
            "prepare_only": True,
            "model": args.model,
            "is_mock": False,
        }
    else:
        provider, provider_metadata = build_provider(
            args.provider,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            retries=args.retries,
            mock_response=args.mock_response,
            max_tokens_field=args.max_tokens_field,
        )

    model = args.model
    if not model and args.provider == "anthropic":
        import os

        model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
    if not model and args.provider not in ("gemini_cli", "mock"):
        parser.error("--model is required for this provider")

    summary = run_benchmark(
        dataset,
        provider,
        provider_name=args.provider,
        model=model,
        modality=args.modality,
        repeats=args.repeats,
        output_dir=output_dir,
        paper_ids=paper_ids,
        limit=args.limit,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        created_at=args.created_at,
        prepare_only=args.prepare_only,
        provider_metadata=provider_metadata,
        concurrency=args.concurrency,
    )
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    compact = {
        "status": summary.get("status", "completed"),
        "run_id": summary.get("run_id"),
        "output_dir": str(output_dir.resolve()),
        "provider": args.provider,
        "model": model,
        "modality": args.modality,
        "repeats": summary.get("repeats"),
        "scope": summary.get("scope"),
        "aggregate": summary.get("aggregate"),
        "attempts": summary.get("attempts"),
        "warning": summary.get("warning"),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
