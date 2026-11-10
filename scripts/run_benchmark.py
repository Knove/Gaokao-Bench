#!/usr/bin/env python3
"""Run a text or required-vision Gaokao-Bench model evaluation."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gaokao_bench.harness import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
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
    parser.add_argument("--data-dir", default="data/0.0.2")
    parser.add_argument(
        "--provider",
        choices=("anthropic", "openai_compatible", "gemini_cli", "mock"),
        required=True,
    )
    parser.add_argument("--model", default="")
    parser.add_argument("--modality", choices=("text", "vision"), default="text")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--view", default="full", help="declared benchmark view ID")
    parser.add_argument("--papers", help="comma-separated paper IDs, e.g. GB25-N1,GB25-BJ")
    parser.add_argument("--limit", type=int, help="first N selected questions, for a pilot only")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help="completion-token ceiling; includes hidden reasoning tokens on many providers",
    )
    parser.add_argument(
        "--reasoning-budget",
        type=int,
        help="optional reasoning-token budget, recorded in the evaluation protocol",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int)
    parser.add_argument(
        "--provider-pin",
        help="comma-separated upstream provider order for OpenRouter-style routing",
    )
    parser.add_argument(
        "--allow-fallbacks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="allow upstream provider fallback when --provider-pin is set",
    )
    parser.add_argument("--extra-body", help="JSON object with provider-specific request fields")
    parser.add_argument("--max-tokens-field", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    parser.add_argument("--api-key-env")
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir")
    parser.add_argument("--report", help="optional path for a copy of summary.json")
    parser.add_argument("--created-at", help="deterministic ISO-8601 timestamp for tests/replays")
    parser.add_argument("--resume", action="store_true", help="resume an interrupted run with the same protocol")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="with --resume, retry provider errors, harness errors, and truncated outputs",
    )
    parser.add_argument("--no-progress", action="store_true", help="disable JSON progress events on stderr")
    parser.add_argument("--prepare-only", action="store_true", help="build and audit prompts without model calls")
    parser.add_argument("--mock-response", default='{"answer": "A"}')
    args = parser.parse_args()

    paper_ids = None
    if args.papers:
        paper_ids = [item.strip() for item in args.papers.split(",") if item.strip()]
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(args.provider, args.modality)
    try:
        dataset = load_dataset(args.data_dir)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.retry_failed and not args.resume:
        parser.error("--retry-failed requires --resume")
    provider_order = None
    if args.provider_pin:
        provider_order = [item.strip() for item in args.provider_pin.split(",") if item.strip()]
    extra_body = {}
    if args.extra_body:
        try:
            extra_body = json.loads(args.extra_body)
        except json.JSONDecodeError as exc:
            parser.error("--extra-body must be a JSON object: %s" % exc)
        if not isinstance(extra_body, dict):
            parser.error("--extra-body must be a JSON object")

    if args.prepare_only:
        provider = FixedResponseProvider(args.mock_response)
        provider_metadata = {
            "provider": args.provider,
            "prepare_only": True,
            "model": args.model,
            "is_mock": False,
            "reasoning_budget": args.reasoning_budget,
            "seed": args.seed,
            "provider_order": provider_order or [],
            "allow_fallbacks": args.allow_fallbacks,
            "extra_body_keys": sorted(extra_body),
            "extra_body_sha256": hashlib.sha256(
                json.dumps(extra_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "max_tokens_field": args.max_tokens_field,
        }
    else:
        provider, provider_metadata = build_provider(
            args.provider,
            api_key_env=args.api_key_env,
            base_url=args.base_url,
            retries=args.retries,
            mock_response=args.mock_response,
            max_tokens_field=args.max_tokens_field,
            reasoning_budget=args.reasoning_budget,
            seed=args.seed,
            provider_order=provider_order,
            allow_fallbacks=args.allow_fallbacks,
            extra_body=extra_body,
        )

    model = args.model
    if not model and args.provider == "anthropic":
        import os

        model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
    if not model and args.provider not in ("gemini_cli", "mock"):
        parser.error("--model is required for this provider")

    progress_callback = None
    if not args.no_progress:
        def progress_callback(event):
            print(json.dumps(event, ensure_ascii=False, sort_keys=True), file=sys.stderr, flush=True)

    try:
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
            view_id=args.view,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
            reasoning_budget=args.reasoning_budget,
            temperature=args.temperature,
            created_at=args.created_at,
            prepare_only=args.prepare_only,
            provider_metadata=provider_metadata,
            concurrency=args.concurrency,
            resume=args.resume,
            retry_failed=args.retry_failed,
            seed=args.seed,
            provider_order=provider_order,
            allow_fallbacks=args.allow_fallbacks,
            progress_callback=progress_callback,
        )
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
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
        "primary_metric": summary.get("primary_metric"),
        "metrics": summary.get("metrics"),
        "aggregate": summary.get("aggregate"),
        "attempts": summary.get("attempts"),
        "warning": summary.get("warning"),
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
