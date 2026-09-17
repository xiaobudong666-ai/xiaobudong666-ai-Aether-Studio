"""Pure M1-C1 platform rule evaluation. Active rules are explicit inputs only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class RuleEvaluation:
    allowed_to_prepare: bool
    manual_review_required: bool
    code: str


def evaluate_rule_pack(
    pack: Mapping[str, Any], *, evaluation_time: datetime, content: Mapping[str, Any]
) -> RuleEvaluation:
    if pack.get("verification_status") != "verified":
        return RuleEvaluation(False, True, "POLICY_RULE_PACK_UNRESOLVED")
    revalidate_after = pack.get("revalidate_after")
    if revalidate_after is not None:
        if not isinstance(revalidate_after, datetime) or evaluation_time > revalidate_after:
            return RuleEvaluation(False, True, "POLICY_RULE_PACK_STALE")
    effective_from = pack.get("effective_from")
    if effective_from is not None and isinstance(effective_from, datetime) and evaluation_time < effective_from:
        return RuleEvaluation(False, True, "POLICY_RULE_NOT_EFFECTIVE")
    effective_to = pack.get("effective_to")
    if effective_to is not None and isinstance(effective_to, datetime) and evaluation_time > effective_to:
        return RuleEvaluation(False, True, "POLICY_RULE_PACK_EXPIRED")
    if not pack.get("source_fingerprint"):
        return RuleEvaluation(False, True, "POLICY_SOURCE_FINGERPRINT_REQUIRED")
    if content.get("provenance_tampered") is True:
        return RuleEvaluation(False, False, "POLICY_PROVENANCE_TAMPER")
    if content.get("ai_generated_or_synthetic") and content.get("public_distribution"):
        if pack.get("jurisdiction_ai_label_required") and not content.get("explicit_label_planned"):
            return RuleEvaluation(False, True, "POLICY_AI_LABEL_REQUIRED")
    if pack.get("platform_overlay_status") in {"missing", "unresolved", "stale"}:
        return RuleEvaluation(False, True, "POLICY_PLATFORM_OVERLAY_REVIEW_REQUIRED")
    return RuleEvaluation(True, False, "POLICY_RULES_SATISFIED")
