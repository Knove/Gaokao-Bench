"""Compact terminal views and machine summaries for benchmark results."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional


def _number(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return ("%.6f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _ratio(value: Any) -> str:
    if value is None:
        return "n/a"
    return "%.2f%%" % (100.0 * float(value))


def _metric_summary(metric: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if not isinstance(metric, Mapping):
        return None
    return {
        key: metric.get(key)
        for key in ("id", "value", "numerator", "denominator", "numerator_unit", "scope", "role")
        if key in metric
    }


def _failure_text(counts: Mapping[str, Any]) -> str:
    failures = {key: value for key, value in counts.items() if key != "correct" and value}
    if not failures:
        return "none"
    return ", ".join("%s=%s" % (key, value) for key, value in sorted(failures.items()))


def _slice_summary(slices: Mapping[str, Any]) -> dict:
    compact = {}
    for name, metric in slices.items():
        if not isinstance(metric, Mapping):
            continue
        row = _metric_summary(metric)
        if row is not None:
            compact[name] = row
    return compact


def compact_evaluation_summary(report: Mapping[str, Any]) -> dict:
    """Return a small JSON payload without duplicate question-ID arrays."""

    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    return {
        "dataset": report.get("dataset"),
        "dataset_version": report.get("dataset_version"),
        "scoring_profile": report.get("scoring_profile"),
        "primary_metric": _metric_summary(summary.get("primary_metric")),
        "points": {
            "earned": summary.get("earned_points"),
            "possible": summary.get("possible_points"),
        },
        "questions": {
            "correct": summary.get("correct_questions"),
            "total": summary.get("total_questions"),
        },
        "failures": {
            key: value
            for key, value in (summary.get("failure_counts") or {}).items()
            if key != "correct" and value
        },
        "slices": _slice_summary(summary.get("slices") or {}),
        "unexpected_answer_ids": list(report.get("unexpected_answer_ids") or []),
    }


def render_evaluation_summary(report: Mapping[str, Any]) -> str:
    """Render a submission score as a short human-readable block."""

    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    primary = summary.get("primary_metric") if isinstance(summary.get("primary_metric"), Mapping) else {}
    lines = [
        "Gaokao-Bench %s · %s" % (report.get("dataset_version"), report.get("scoring_profile")),
        "Primary  %s  %s  (%s / %s points)"
        % (
            primary.get("id", "n/a"),
            _ratio(primary.get("value")),
            _number(primary.get("numerator")),
            _number(primary.get("denominator")),
        ),
        "Questions  %s / %s exact  ·  failures: %s"
        % (
            _number(summary.get("correct_questions")),
            _number(summary.get("total_questions")),
            _failure_text(summary.get("failure_counts") or {}),
        ),
        "Scope  %s" % primary.get("scope", "n/a"),
    ]

    labels = (("text_answerable", "Text"), ("vision_required", "Vision"))
    slices = summary.get("slices") if isinstance(summary.get("slices"), Mapping) else {}
    for key, label in labels:
        metric = slices.get(key)
        if isinstance(metric, Mapping) and metric.get("value") is not None:
            lines.append(
                "%-6s  %s  (%s / %s points)"
                % (
                    label,
                    _ratio(metric.get("value")),
                    _number(metric.get("numerator")),
                    _number(metric.get("denominator")),
                )
            )

    papers = summary.get("papers") if isinstance(summary.get("papers"), Mapping) else {}
    if papers:
        lines.extend(["", "Papers"])
        for paper_id in sorted(papers):
            paper = papers[paper_id]
            if not isinstance(paper, Mapping):
                continue
            lines.append(
                "  %s  %s/%s points  %s/%s exact"
                % (
                    paper.get("title") or paper_id,
                    _number(paper.get("earned_points")),
                    _number(paper.get("possible_points")),
                    _number(paper.get("correct_questions")),
                    _number(paper.get("questions")),
                )
            )

    unexpected = report.get("unexpected_answer_ids") or []
    if unexpected:
        lines.append("Unexpected answer IDs  %d" % len(unexpected))
    return "\n".join(lines)


def compact_run_summary(summary: Mapping[str, Any]) -> dict:
    """Return a run-level JSON view with scope IDs removed."""

    status = summary.get("status")
    aggregate = summary.get("aggregate") if isinstance(summary.get("aggregate"), Mapping) else {}
    reproducibility = (
        summary.get("reproducibility")
        if isinstance(summary.get("reproducibility"), Mapping)
        else {}
    )
    compact = {
        "dataset": summary.get("dataset"),
        "dataset_version": summary.get("dataset_version"),
        "status": status,
        "result_status": summary.get("result_status", status),
        "run_id": summary.get("run_id"),
        "provider": summary.get("provider"),
        "model": summary.get("model"),
        "model_snapshot": summary.get("model_snapshot"),
        "modality": summary.get("modality"),
        "repeats": summary.get("repeats"),
    }
    scope = summary.get("scope") if isinstance(summary.get("scope"), Mapping) else summary
    compact["scope"] = {
        key: scope.get(key)
        for key in (
            "coverage_label",
            "kind",
            "view",
            "selected_papers",
            "selected_questions",
            "dataset_questions",
            "selected_possible_points",
            "dataset_possible_points",
            "modality_answerable_questions",
            "modality_answerable_points",
        )
    }
    if status == "prepared":
        compact.update(
            {
                "prompt_manifest": summary.get("prompt_manifest"),
                "prompt_version": summary.get("prompt_version"),
                "prompt_renderer_version": summary.get("prompt_renderer_version"),
                "versions": summary.get("versions"),
                "compatibility": summary.get("compatibility"),
            }
        )
        return compact
    compact.update(
        {
            "aggregate": {
                "earned_points": aggregate.get("earned_points"),
                "correct_questions": aggregate.get("correct_questions"),
                "possible_points": aggregate.get("possible_points"),
                "questions": aggregate.get("questions"),
                "borderline_questions": aggregate.get("borderline_questions"),
                "borderline_candidate_questions": aggregate.get(
                    "borderline_candidate_questions"
                ),
                "primary_metric": aggregate.get("primary_metric"),
                "slices": _slice_summary(aggregate.get("slices") or {}),
            },
            "primary_metric": _metric_summary(summary.get("primary_metric")),
            "slices": _slice_summary(summary.get("slices") or {}),
            "modality_summary": summary.get("modality_summary"),
            "versions": summary.get("versions"),
            "compatibility": summary.get("compatibility"),
            "reproducibility": {
                key: reproducibility.get(key)
                for key in (
                    "status",
                    "canonical_eligible",
                    "repeats",
                    "repetitions_met",
                    "question_flips",
                    "provider_routing",
                    "seed",
                    "system_fingerprints",
                    "caveats",
                )
                if key in reproducibility
            },
            "attempts": summary.get("attempts"),
            "usage": summary.get("usage"),
            "leaderboard": summary.get("leaderboard"),
            "warning": summary.get("warning"),
        }
    )
    return compact


def render_run_summary(
    summary: Mapping[str, Any],
    *,
    output_dir: Optional[Path | str] = None,
    report_path: Optional[Path | str] = None,
) -> str:
    """Render a benchmark run as a short human-readable block."""

    status = summary.get("status")
    result_status = summary.get("result_status", status)
    versions = summary.get("versions") if isinstance(summary.get("versions"), Mapping) else {}
    header = "Gaokao-Bench %s · %s/%s · %s · %s" % (
        summary.get("dataset_version"),
        summary.get("provider"),
        summary.get("model"),
        summary.get("modality"),
        result_status,
    )
    version_line = "Versions  dataset %s · engine %s · harness %s · scorer %s · spec %s" % (
        versions.get("dataset", "n/a"),
        versions.get("engine", "n/a"),
        versions.get("harness", "n/a"),
        versions.get("scorer", "n/a"),
        versions.get("benchmark_spec", "n/a"),
    )
    if status == "prepared":
        selected_papers = summary.get("selected_papers") or []
        scope_text = "%s question%s / %s points" % (
            _number(summary.get("selected_questions")),
            "" if summary.get("selected_questions") == 1 else "s",
            _number(summary.get("selected_possible_points")),
        )
        if selected_papers:
            scope_text += " · papers %s" % ", ".join(str(item) for item in selected_papers)
        lines = [
            header,
            "Run  %s" % summary.get("run_id", "n/a"),
            "Prepared  %s" % scope_text,
            "Prompts  %s rendered · no model calls" % _number(summary.get("selected_questions")),
        ]
        if versions:
            lines.append(version_line)
        if output_dir is not None:
            lines.append("Prompt preview  %s" % (Path(output_dir) / "prompts.jsonl"))
            lines.append("Run directory  %s" % Path(output_dir))
        if report_path is not None:
            lines.append("Report  %s" % Path(report_path))
        return "\n".join(lines)

    aggregate = summary.get("aggregate") if isinstance(summary.get("aggregate"), Mapping) else {}
    metric = summary.get("primary_metric") if isinstance(summary.get("primary_metric"), Mapping) else {}
    metric_agg = aggregate.get("primary_metric") if isinstance(aggregate.get("primary_metric"), Mapping) else {}
    earned = aggregate.get("earned_points") if isinstance(aggregate.get("earned_points"), Mapping) else {}
    correct = (
        aggregate.get("correct_questions")
        if isinstance(aggregate.get("correct_questions"), Mapping)
        else {}
    )
    attempts = summary.get("attempts") if isinstance(summary.get("attempts"), Mapping) else {}
    reproducibility = (
        summary.get("reproducibility")
        if isinstance(summary.get("reproducibility"), Mapping)
        else {}
    )
    lines = [
        header,
        "Run  %s" % summary.get("run_id", "n/a"),
        "Primary  %s  %s mean  (min %s, max %s)"
        % (
            metric.get("id", "n/a"),
            _ratio(metric_agg.get("mean")),
            _ratio(metric_agg.get("min")),
            _ratio(metric_agg.get("max")),
        ),
        "Points  %s mean / %s possible  ·  questions %s / %s exact"
        % (
            _number(earned.get("mean")),
            _number(aggregate.get("possible_points")),
            _number(correct.get("mean")),
            _number(aggregate.get("questions")),
        ),
    ]
    if versions:
        lines.append(version_line)
    slices = summary.get("slices") if isinstance(summary.get("slices"), Mapping) else {}
    for key, label in (("text_answerable", "Text"), ("vision_required", "Vision")):
        slice_metric = slices.get(key)
        if isinstance(slice_metric, Mapping) and slice_metric.get("value") is not None:
            lines.append(
                "%-6s  %s  (%s / %s points)"
                % (
                    label,
                    _ratio(slice_metric.get("value")),
                    _number(slice_metric.get("numerator")),
                    _number(slice_metric.get("denominator")),
                )
            )
    lines.append(
        "Attempts  %s  ·  parse errors %s  ·  truncations %s"
        % (
            _number(attempts.get("total")),
            _number(attempts.get("parse_errors")),
            _number(attempts.get("truncations")),
        )
    )
    caveats = reproducibility.get("caveats") or []
    lines.append(
        "Reproducibility  %s%s"
        % (
            reproducibility.get("status", "n/a"),
            " · %s" % "; ".join(str(item) for item in caveats) if caveats else "",
        )
    )
    if output_dir is not None:
        lines.append("Run directory  %s" % Path(output_dir))
    if report_path is not None:
        lines.append("Report  %s" % Path(report_path))
    return "\n".join(lines)
