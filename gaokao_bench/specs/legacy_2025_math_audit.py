"""Data-specific evidence gate for the published 0.0.1 mathematics package.

The generic engine validates source-assessment shape.  This adapter keeps the
historical Q13 dispute decisions auditable without teaching the engine about a
specific 2025 question.
"""

from __future__ import annotations

from typing import Any, List, Mapping


Q13_ID = "GB25-N1-Q13"


def validate_q13_gate(row: Mapping[str, Any], errors: List[str]) -> None:
    qid = str(row.get("question_id", ""))
    if qid != Q13_ID:
        return

    assessments = row.get("source_assessments")
    if not isinstance(assessments, list) or not assessments:
        errors.append("Q13 must carry source assessments with stem-consistency decisions")
        return
    valid = [item for item in assessments if isinstance(item, dict)]
    supporting = [item for item in valid if item.get("role") == "supporting"]
    rejected = [item for item in valid if item.get("role") == "rejected"]
    exact = [item for item in supporting if item.get("stem_match") == "exact_stem_match"]
    publishers = {str(item.get("publisher", "")).strip() for item in supporting}
    independence_groups = {
        str(item.get("independence_group", "")).strip() for item in supporting
    }

    if str(row.get("canonical") if "canonical" in row else row.get("reviewed_canonical")) != "2":
        errors.append("Q13 canonical answer must be 2")
    if row.get("status") not in {
        "source_and_derivation_confirmed",
        "owner_and_two_independent_reviewers_complete",
    }:
        errors.append("Q13 audit must retain source-and-derivation evidence before or after signoff")
    if str(row.get("external_canonical")) != "2" or str(row.get("secondary_external_canonical")) != "2":
        errors.append("Q13 visible source answer values must both be 2")
    if len(supporting) < 4:
        errors.append("Q13 requires at least four supporting source assessments")
    if len(exact) < 3:
        errors.append("Q13 requires at least three exact-stem source assessments")
    if len(publishers) < 3:
        errors.append("Q13 requires supporting evidence from at least three publishers")
    if len(independence_groups) < 3:
        errors.append("Q13 requires supporting evidence from at least three independence groups")
    if any(
        item.get("stem_match") in ("variant_missing_positive_condition", "stem_not_visible")
        for item in supporting
    ):
        errors.append("Q13 supporting evidence includes a source without a matching stem")

    rejected_ids = {str(item.get("source_id")) for item in rejected}
    rejected_reasons = {str(item.get("stem_match")) for item in rejected}
    required_rejections = {"stem_not_visible", "variant_missing_positive_condition"}
    if len(rejected) < 2 or not required_rejections <= rejected_reasons:
        errors.append(
            "Q13 must preserve rejected sources for an invisible stem and a variant without the positive condition"
        )
    declared_rejected = {str(source_id) for source_id in (row.get("rejected_source_ids") or [])}
    if declared_rejected != rejected_ids:
        errors.append("Q13 rejected_source_ids must match rejected source assessments")
    supporting_ids = {
        str(row.get("external_source_id")),
        str(row.get("secondary_source_id")),
        *(str(source_id) for source_id in (row.get("supporting_source_ids") or [])),
    }
    assessed_supporting_ids = {str(item.get("source_id")) for item in supporting}
    if supporting_ids != assessed_supporting_ids:
        errors.append("Q13 declared supporting source IDs must match supporting assessments")
