"""Shared quality-level contracts for auditable dataset releases.

The quality level is deliberately independent from the dataset version.  A
new release can change questions while retaining the same evidence standard,
and consumers can inspect one field instead of inferring the review protocol
from a collection of status strings.
"""

from __future__ import annotations

from typing import Any, Mapping


STRICT_DUAL_SOURCE_V4 = "strict_dual_source_v4"
# A mixed release carries papers whose evidence is complete next to papers whose
# evidence is still thin.  Instead of pretending one level covers the whole
# package, the mixed level records the achieved evidence tier for every single
# question and enumerates the subset that really meets the strict floor.
MIXED_SOURCE_AUDIT_V1 = "mixed_source_audit_v1"
QUALITY_LEVEL = STRICT_DUAL_SOURCE_V4
SOURCE_AUDIT_POLICY_VERSION = STRICT_DUAL_SOURCE_V4
REQUIRED_STEM_INDEPENDENT_GROUPS = 9
REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS = 6

# per-question evidence tiers, from strongest to weakest
EVIDENCE_LEVEL_STRICT = STRICT_DUAL_SOURCE_V4
EVIDENCE_LEVEL_DUAL = "dual_source_v2"
EVIDENCE_LEVEL_STEM_CORROBORATED = "stem_corroborated_v1"
EVIDENCE_LEVEL_SINGLE = "single_source_v1"
EVIDENCE_LEVELS = (
    EVIDENCE_LEVEL_STRICT,
    EVIDENCE_LEVEL_DUAL,
    EVIDENCE_LEVEL_STEM_CORROBORATED,
    EVIDENCE_LEVEL_SINGLE,
)
DUAL_SOURCE_STEM_GROUPS = 2
DUAL_SOURCE_JOINT_GROUPS = 2


def evidence_level(*, stem_groups: int, joint_groups: int) -> str:
    """Return the achieved evidence tier for one question."""

    if (
        stem_groups >= REQUIRED_STEM_INDEPENDENT_GROUPS
        and joint_groups >= REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS
    ):
        return EVIDENCE_LEVEL_STRICT
    if stem_groups >= DUAL_SOURCE_STEM_GROUPS and joint_groups >= DUAL_SOURCE_JOINT_GROUPS:
        return EVIDENCE_LEVEL_DUAL
    if stem_groups >= DUAL_SOURCE_STEM_GROUPS:
        return EVIDENCE_LEVEL_STEM_CORROBORATED
    return EVIDENCE_LEVEL_SINGLE


def source_audit_policy(*, required_question_count: int | None = None) -> dict[str, Any]:
    """Return the canonical policy object for a strictly audited release."""

    policy: dict[str, Any] = {
        "version": SOURCE_AUDIT_POLICY_VERSION,
        "scope": "all_catalogued_questions",
        "required_stem_independent_groups": REQUIRED_STEM_INDEPENDENT_GROUPS,
        "required_joint_answer_independent_groups": (
            REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS
        ),
        "answer_only_sources_count_for_gate": False,
        "owner_signoff_requires_gate": True,
        "joint_answer_source_must_match_stem": True,
        "required_source_page_evidence": True,
    }
    if required_question_count is not None:
        policy["required_question_count"] = int(required_question_count)
    return policy


def mixed_source_audit_policy(
    *,
    required_question_count: int | None = None,
    strict_questions: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the policy object for a package with per-question evidence tiers."""

    policy: dict[str, Any] = {
        "version": MIXED_SOURCE_AUDIT_V1,
        "scope": "all_catalogued_questions",
        "minimum_stem_independent_groups": 1,
        "strict_level": STRICT_DUAL_SOURCE_V4,
        "strict_stem_independent_groups": REQUIRED_STEM_INDEPENDENT_GROUPS,
        "strict_joint_answer_independent_groups": (
            REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS
        ),
        "strict_questions": sorted(str(qid) for qid in strict_questions),
        "answer_only_sources_count_for_gate": False,
        "joint_answer_source_must_match_stem": True,
        "required_source_page_evidence": True,
        "owner_signoff_requires_gate": False,
    }
    if required_question_count is not None:
        policy["required_question_count"] = int(required_question_count)
    return policy


def quality_level_errors(
    manifest: Mapping[str, Any],
    *,
    require_source_audit_quality: bool = False,
) -> list[str]:
    """Return consistency errors for a declared quality level.

    Historical packages without a quality level remain readable.  Callers
    that are about to publish or execute a source-audited release should pass
    ``require_source_audit_quality=True``.
    """

    quality = manifest.get("quality")
    quality = quality if isinstance(quality, Mapping) else {}
    declared = quality.get("quality_level")
    source_audit = manifest.get("source_audit")
    source_audit = source_audit if isinstance(source_audit, Mapping) else None
    if declared is None:
        if require_source_audit_quality and source_audit is not None:
            return ["manifest.quality.quality_level is required for a source-audited release"]
        return []
    if declared == MIXED_SOURCE_AUDIT_V1:
        return _mixed_level_errors(manifest, source_audit)
    if declared != QUALITY_LEVEL:
        return [
            "manifest.quality.quality_level must be %s or %s, got %s"
            % (QUALITY_LEVEL, MIXED_SOURCE_AUDIT_V1, declared)
        ]
    if source_audit is None:
        return ["a strict quality level requires manifest.source_audit"]
    policy = source_audit.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    errors: list[str] = []
    if policy.get("version") != SOURCE_AUDIT_POLICY_VERSION:
        errors.append(
            "source audit policy version must match quality level %s"
            % QUALITY_LEVEL
        )
    stem_floor = int(policy.get("required_stem_independent_groups") or 0)
    joint_floor = int(policy.get("required_joint_answer_independent_groups") or 0)
    if stem_floor < REQUIRED_STEM_INDEPENDENT_GROUPS:
        errors.append(
            "strict quality level requires at least nine independent stem groups"
        )
    if joint_floor < REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS:
        errors.append(
            "strict quality level requires at least six independent joint groups"
        )
    if policy.get("answer_only_sources_count_for_gate") is not False:
        errors.append("strict quality level forbids answer-only sources in the gate")
    if policy.get("joint_answer_source_must_match_stem") is not True:
        errors.append("strict quality level requires joint sources to match the stem")
    if policy.get("required_source_page_evidence") is not True:
        errors.append("strict quality level requires frozen source-page evidence")
    return errors


def _mixed_level_errors(
    manifest: Mapping[str, Any], source_audit: Mapping[str, Any] | None
) -> list[str]:
    """Check the mixed level: every question evidenced, tiers told truthfully."""

    if source_audit is None:
        return ["a mixed quality level requires manifest.source_audit"]
    policy = source_audit.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    errors: list[str] = []
    if policy.get("version") != MIXED_SOURCE_AUDIT_V1:
        errors.append(
            "source audit policy version must match quality level %s"
            % MIXED_SOURCE_AUDIT_V1
        )
    if int(policy.get("minimum_stem_independent_groups") or 0) < 1:
        errors.append("mixed quality level requires at least one independent stem group")
    if policy.get("answer_only_sources_count_for_gate") is not False:
        errors.append("answer-only sources must not count toward the stem gate")
    if policy.get("joint_answer_source_must_match_stem") is not True:
        errors.append("a mixed quality level requires joint sources to match the stem")
    if policy.get("required_source_page_evidence") is not True:
        errors.append("a mixed quality level requires frozen source-page evidence")
    if policy.get("strict_level") != STRICT_DUAL_SOURCE_V4:
        errors.append("a mixed quality level must name its strict tier")
    for key, floor in (
        ("strict_stem_independent_groups", REQUIRED_STEM_INDEPENDENT_GROUPS),
        (
            "strict_joint_answer_independent_groups",
            REQUIRED_JOINT_ANSWER_INDEPENDENT_GROUPS,
        ),
    ):
        if int(policy.get(key) or 0) < floor:
            errors.append("a mixed quality level must keep the strict floor for %s" % key)
    strict_questions = policy.get("strict_questions")
    if not isinstance(strict_questions, list):
        errors.append("a mixed quality level must enumerate its strict questions")
    return errors
