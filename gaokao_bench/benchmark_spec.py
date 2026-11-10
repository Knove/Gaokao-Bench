"""Declarative benchmark specifications.

The evaluation engine must not know which year, subject, paper, or question
count a dataset contains.  Those choices belong to a versioned benchmark
specification.  This module parses that specification and supplies a small,
explicit compatibility adapter for the already-published ``0.0.1`` package.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SPEC_SCHEMA_VERSION = "1.0"
BENCHMARK_SPEC_FILENAME = "benchmark_spec.json"


class BenchmarkSpecError(ValueError):
    """Raised when a benchmark specification is missing or malformed."""


@dataclass(frozen=True)
class AnswerShapeSpec:
    shape: str
    min_selections: Optional[int] = None
    max_selections: Optional[int] = None
    values: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectedSpec:
    question_count: Optional[int] = None
    paper_count: Optional[int] = None
    total_points: Optional[int] = None
    paper_points: Mapping[str, int] = field(default_factory=dict)
    full_paper_question_count: Optional[int] = None
    full_paper_points: Optional[int] = None
    task_type_counts: Mapping[str, int] = field(default_factory=dict)
    catalog_question_count: Optional[int] = None
    catalog_points: Optional[int] = None
    required_image_ids: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class ViewSpec:
    id: str
    description: str
    selector: Mapping[str, Any]


@dataclass(frozen=True)
class PromptSpec:
    version: str
    system: str
    answer_schema: str = "gaokao-answer-v1"
    image_crops: Mapping[str, Tuple[int, int, int, int]] = field(default_factory=dict)
    image_pixel_sha256: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoringSpec:
    profile: str
    metric_version: str
    primary_metric: str = "score.weighted_points"
    partial_credit: bool = False


@dataclass(frozen=True)
class BenchmarkSpec:
    id: str
    version: str
    subject: str
    years: Tuple[int, ...]
    task_types: Mapping[str, AnswerShapeSpec]
    choice_labels: Tuple[str, ...]
    expected: ExpectedSpec
    views: Mapping[str, ViewSpec]
    prompt: PromptSpec
    scoring: ScoringSpec
    validators: Tuple[str, ...]
    custom_validators: Tuple[str, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def schema_version(self) -> str:
        return str(self.raw.get("schema_version", SPEC_SCHEMA_VERSION))

    def answer_shape(self, question_type: str) -> Optional[AnswerShapeSpec]:
        return self.task_types.get(question_type)

    def fingerprint(self) -> str:
        payload = json.dumps(self.raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def select_view(self, view_id: str, questions: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
        view = self.views.get(view_id)
        if view is None:
            raise BenchmarkSpecError("unknown benchmark view: %s" % view_id)
        return [question for question in questions if _matches_selector(question, view.selector)]


def question_requires_vision(question: Mapping[str, Any]) -> bool:
    """Return whether a question needs an attached visual asset.

    The legacy package records this as ``asset_dependency=required``.  New
    subject tracks can declare ``vision_required`` directly or use input
    modalities with ``text_sufficient=false``.  Keeping the rule in one place
    prevents every subject validator and metric from inventing its own test.
    """

    if question.get("vision_required") is True:
        return True
    if question.get("asset_dependency") == "required":
        return True
    modalities = question.get("input_modalities")
    if isinstance(modalities, (list, tuple)) and "image" in modalities:
        return question.get("text_sufficient") is not True
    return False


def question_type(question: Mapping[str, Any]) -> str:
    """Return the declared task type while accepting both schema names.

    ``type`` is the field used by the published 0.0.1 package.  New data
    packages may use the more explicit ``task_type``; runtime code treats them
    as aliases so a subject migration does not require a second engine.
    """

    value = question.get("task_type") or question.get("type") or ""
    return str(value)


def _matches_selector(question: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    if not selector or selector.get("all") is True:
        return True

    for key, expected in selector.items():
        if key in ("all", "description"):
            continue
        actual = question.get(key)
        if isinstance(expected, Mapping):
            if "not" in expected and actual == expected["not"]:
                return False
            if "in" in expected and actual not in expected["in"]:
                return False
            if "contains" in expected:
                if not isinstance(actual, (list, tuple, set)) or expected["contains"] not in actual:
                    return False
            if "required" in expected and bool(expected["required"]) != bool(actual):
                return False
        elif actual != expected:
            return False
    return True


def _int_or_none(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchmarkSpecError("%s must be an integer or null" % field_name)
    return value


def _tuple_of_strings(value: Any, field_name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
        raise BenchmarkSpecError("%s must be an array of non-empty strings" % field_name)
    return tuple(value)


def _parse_task_types(raw: Any) -> Dict[str, AnswerShapeSpec]:
    if not isinstance(raw, Mapping) or not raw:
        raise BenchmarkSpecError("task_types must be a non-empty object")
    parsed: Dict[str, AnswerShapeSpec] = {}
    for question_type, definition in raw.items():
        if not isinstance(question_type, str) or not question_type:
            raise BenchmarkSpecError("task type names must be non-empty strings")
        if not isinstance(definition, Mapping):
            raise BenchmarkSpecError("task type %s must be an object" % question_type)
        shape = str(definition.get("shape", ""))
        if shape not in ("choice", "slots", "rubric"):
            raise BenchmarkSpecError("task type %s has unsupported answer shape %r" % (question_type, shape))
        parsed[question_type] = AnswerShapeSpec(
            shape=shape,
            min_selections=_int_or_none(definition.get("min_selections"), "min_selections"),
            max_selections=_int_or_none(definition.get("max_selections"), "max_selections"),
            values=_tuple_of_strings(definition.get("values"), "task type values"),
        )
    return parsed


def _parse_expected(raw: Any) -> ExpectedSpec:
    if raw is None:
        return ExpectedSpec()
    if not isinstance(raw, Mapping):
        raise BenchmarkSpecError("expected must be an object")
    paper_points_raw = raw.get("paper_points") or {}
    if not isinstance(paper_points_raw, Mapping):
        raise BenchmarkSpecError("expected.paper_points must be an object")
    paper_points: Dict[str, int] = {}
    for paper_id, points in paper_points_raw.items():
        if not isinstance(paper_id, str) or isinstance(points, bool) or not isinstance(points, int):
            raise BenchmarkSpecError("expected.paper_points must map paper IDs to integers")
        paper_points[paper_id] = points
    task_counts_raw = raw.get("task_type_counts") or {}
    if not isinstance(task_counts_raw, Mapping):
        raise BenchmarkSpecError("expected.task_type_counts must be an object")
    task_counts: Dict[str, int] = {}
    for question_type, count in task_counts_raw.items():
        if not isinstance(question_type, str) or isinstance(count, bool) or not isinstance(count, int):
            raise BenchmarkSpecError("expected.task_type_counts must map task types to integers")
        task_counts[question_type] = count
    required_image_ids = raw.get("required_image_ids")
    return ExpectedSpec(
        question_count=_int_or_none(raw.get("question_count"), "expected.question_count"),
        paper_count=_int_or_none(raw.get("paper_count"), "expected.paper_count"),
        total_points=_int_or_none(raw.get("total_points"), "expected.total_points"),
        paper_points=paper_points,
        full_paper_question_count=_int_or_none(
            raw.get("full_paper_question_count"), "expected.full_paper_question_count"
        ),
        full_paper_points=_int_or_none(raw.get("full_paper_points"), "expected.full_paper_points"),
        task_type_counts=task_counts,
        catalog_question_count=_int_or_none(
            raw.get("catalog_question_count"), "expected.catalog_question_count"
        ),
        catalog_points=_int_or_none(raw.get("catalog_points"), "expected.catalog_points"),
        required_image_ids=(
            None if required_image_ids is None else _tuple_of_strings(required_image_ids, "required_image_ids")
        ),
    )


def _parse_views(raw: Any) -> Dict[str, ViewSpec]:
    if raw is None:
        return {"full": ViewSpec("full", "All scored questions in the data package", {"all": True})}
    if not isinstance(raw, list) or not raw:
        raise BenchmarkSpecError("views must be a non-empty array")
    parsed: Dict[str, ViewSpec] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise BenchmarkSpecError("view %d must be an object" % index)
        view_id = str(item.get("id", ""))
        selector = item.get("selector")
        if not view_id or not isinstance(selector, Mapping):
            raise BenchmarkSpecError("view %d needs id and selector" % index)
        if view_id in parsed:
            raise BenchmarkSpecError("duplicate benchmark view: %s" % view_id)
        parsed[view_id] = ViewSpec(
            id=view_id,
            description=str(item.get("description", "")),
            selector=dict(selector),
        )
    return parsed


def _parse_prompt(raw: Any) -> PromptSpec:
    if not isinstance(raw, Mapping):
        raise BenchmarkSpecError("prompt must be an object")
    version = str(raw.get("version", ""))
    system = str(raw.get("system", ""))
    if not version or not system.strip():
        raise BenchmarkSpecError("prompt.version and prompt.system must be non-empty")
    crops: Dict[str, Tuple[int, int, int, int]] = {}
    raw_crops = raw.get("image_crops") or {}
    if not isinstance(raw_crops, Mapping):
        raise BenchmarkSpecError("prompt.image_crops must be an object")
    for qid, box in raw_crops.items():
        if not isinstance(box, (list, tuple)) or len(box) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in box
        ):
            raise BenchmarkSpecError("prompt crop for %s must contain four integers" % qid)
        crops[str(qid)] = tuple(int(value) for value in box)  # type: ignore[assignment]
    hashes_raw = raw.get("image_pixel_sha256") or {}
    if not isinstance(hashes_raw, Mapping):
        raise BenchmarkSpecError("prompt.image_pixel_sha256 must be an object")
    return PromptSpec(
        version=version,
        system=system,
        answer_schema=str(raw.get("answer_schema", "gaokao-answer-v1")),
        image_crops=crops,
        image_pixel_sha256={str(key): str(value) for key, value in hashes_raw.items()},
    )


def parse_benchmark_spec(raw: Mapping[str, Any]) -> BenchmarkSpec:
    if not isinstance(raw, Mapping):
        raise BenchmarkSpecError("benchmark spec must be an object")
    schema_version = str(raw.get("schema_version", SPEC_SCHEMA_VERSION))
    if schema_version != SPEC_SCHEMA_VERSION:
        raise BenchmarkSpecError(
            "unsupported benchmark spec schema_version: %s" % schema_version
        )
    spec_id = str(raw.get("id", ""))
    version = str(raw.get("version", ""))
    subject = str(raw.get("subject", ""))
    years_raw = raw.get("years")
    if not spec_id or not version or not subject:
        raise BenchmarkSpecError("benchmark spec id, version, and subject are required")
    if not isinstance(years_raw, list) or not years_raw or any(
        isinstance(year, bool) or not isinstance(year, int) for year in years_raw
    ):
        raise BenchmarkSpecError("benchmark spec years must be a non-empty integer array")
    scoring_raw = raw.get("scoring")
    if not isinstance(scoring_raw, Mapping):
        raise BenchmarkSpecError("scoring must be an object")
    scoring = ScoringSpec(
        profile=str(scoring_raw.get("profile", "")),
        metric_version=str(scoring_raw.get("metric_version", "1.0")),
        primary_metric=str(scoring_raw.get("primary_metric", "score.weighted_points")),
        partial_credit=bool(scoring_raw.get("partial_credit", False)),
    )
    if not scoring.profile:
        raise BenchmarkSpecError("scoring.profile is required")
    choice_labels = _tuple_of_strings(raw.get("choice_labels"), "choice_labels") or tuple("ABCD")
    if len(set(choice_labels)) != len(choice_labels):
        raise BenchmarkSpecError("choice_labels must be unique")
    return BenchmarkSpec(
        id=spec_id,
        version=version,
        subject=subject,
        years=tuple(int(year) for year in years_raw),
        task_types=_parse_task_types(raw.get("task_types")),
        choice_labels=choice_labels,
        expected=_parse_expected(raw.get("expected")),
        views=_parse_views(raw.get("views")),
        prompt=_parse_prompt(raw.get("prompt")),
        scoring=scoring,
        validators=_tuple_of_strings(raw.get("validators"), "validators"),
        custom_validators=_tuple_of_strings(raw.get("custom_validators"), "custom_validators"),
        raw=dict(raw),
    )


def load_benchmark_spec(
    data_dir: Path | str,
    manifest: Mapping[str, Any],
) -> BenchmarkSpec:
    """Load an inline spec, a spec file, or the explicit legacy adapter."""

    declared = manifest.get("benchmark_spec")
    if isinstance(declared, Mapping):
        return parse_benchmark_spec(declared)
    if isinstance(declared, str) and declared.strip():
        relative = Path(declared)
        if relative.is_absolute() or ".." in relative.parts:
            raise BenchmarkSpecError("benchmark_spec path must stay inside the data package")
        path = Path(data_dir) / relative
        if not path.is_file():
            raise BenchmarkSpecError("benchmark_spec file is missing: %s" % relative)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BenchmarkSpecError("invalid benchmark_spec JSON: %s" % exc) from exc
        return parse_benchmark_spec(raw)
    if declared is not None:
        raise BenchmarkSpecError("benchmark_spec must be an object or a relative path")

    from .specs.legacy_2025_math import LEGACY_SPEC, matches_manifest

    if not matches_manifest(manifest):
        raise BenchmarkSpecError(
            "dataset does not declare benchmark_spec and does not match the legacy 2025 mathematics adapter"
        )
    return LEGACY_SPEC
