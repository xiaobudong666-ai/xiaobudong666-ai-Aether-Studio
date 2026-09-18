"""Pure M1-C1 quality/release gate. Findings never self-approve release."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Any


_SEVERITY = {
    "PASS": 0,
    "ADVISORY": 1,
    "REVIEW_REQUIRED": 2,
    "STALE_RECOMPUTE_REQUIRED": 3,
    "BLOCK_OVERRIDABLE": 4,
    "BLOCK_NON_OVERRIDABLE": 5,
}


@dataclass(frozen=True)
class GateResult:
    eligible_for_human_release: bool
    aggregate: str
    code: str


def evaluate_quality_release(findings: Iterable[Mapping[str, Any]]) -> GateResult:
    aggregate = "PASS"
    seen = False
    for finding in findings:
        seen = True
        state = str(finding.get("state", "REVIEW_REQUIRED"))
        if state not in _SEVERITY:
            state = "REVIEW_REQUIRED"
        if _SEVERITY[state] > _SEVERITY[aggregate]:
            aggregate = state
    if not seen:
        return GateResult(False, "REVIEW_REQUIRED", "QUALITY_EVIDENCE_MISSING")
    if aggregate in {"BLOCK_NON_OVERRIDABLE", "BLOCK_OVERRIDABLE", "STALE_RECOMPUTE_REQUIRED", "REVIEW_REQUIRED"}:
        return GateResult(False, aggregate, "QUALITY_RELEASE_BLOCKED")
    return GateResult(True, aggregate, "QUALITY_READY_FOR_HUMAN_RELEASE")


def validate_human_release_decision(
    decision: Mapping[str, Any],
    *,
    timeline_digest: str,
    preview_evidence_ref: str,
    qa_summary_digest: str,
    rule_pack_digest: str,
    rights_evidence_digest: str,
    parity_policy_version: str,
) -> GateResult:
    if decision.get("actor_type") != "human":
        return GateResult(False, "BLOCK_NON_OVERRIDABLE", "RELEASE_DECISION_HUMAN_REQUIRED")
    required = {
        "timeline_digest": timeline_digest,
        "preview_evidence_ref": preview_evidence_ref,
        "qa_summary_digest": qa_summary_digest,
        "rule_pack_digest": rule_pack_digest,
        "rights_evidence_digest": rights_evidence_digest,
        "parity_policy_version": parity_policy_version,
    }
    for key, expected in required.items():
        if decision.get(key) != expected:
            return GateResult(False, "STALE_RECOMPUTE_REQUIRED", "RELEASE_DECISION_EVIDENCE_STALE")
    if decision.get("decision") != "APPROVE":
        return GateResult(False, "BLOCK_OVERRIDABLE", "RELEASE_DECISION_NOT_APPROVED")
    return GateResult(True, "PASS", "HUMAN_RELEASE_VALID")
