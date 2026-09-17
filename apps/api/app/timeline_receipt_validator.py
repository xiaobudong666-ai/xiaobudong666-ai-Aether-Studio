"""Pure M1-C1 timeline receipt validation. No I/O, retries, or mutations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    code: str
    deduplicated: bool = False
    returned_version: int | None = None


def validate_timeline_receipt(
    receipt: Mapping[str, Any],
    *,
    current_version: int,
    prior_commit: Mapping[str, Any] | None = None,
) -> ValidationResult:
    """Validate CAS, atomicity and idempotency without changing timeline state."""
    idem = receipt.get("idempotency_key")
    if not idem:
        return ValidationResult(False, "TIMELINE_IDEMPOTENCY_KEY_REQUIRED")

    if prior_commit and prior_commit.get("idempotency_key") == idem:
        same = (
            prior_commit.get("timeline_version_before") == receipt.get("timeline_version_before")
            and prior_commit.get("timeline_version_after") == receipt.get("timeline_version_after")
            and prior_commit.get("timeline_digest_after") == receipt.get("timeline_digest_after")
        )
        if not same:
            return ValidationResult(False, "TIMELINE_IDEMPOTENCY_CONFLICT")
        return ValidationResult(
            True,
            "TIMELINE_IDEMPOTENT_REPLAY",
            deduplicated=True,
            returned_version=prior_commit.get("timeline_version_after"),
        )

    before = receipt.get("timeline_version_before")
    after = receipt.get("timeline_version_after")
    if before != current_version:
        return ValidationResult(False, "TIMELINE_VERSION_CONFLICT")
    if not isinstance(after, int) or after != current_version + 1:
        return ValidationResult(False, "TIMELINE_VERSION_ADVANCE_INVALID")
    if receipt.get("operation_type") == "batch" and receipt.get("batch_result") != "success":
        return ValidationResult(False, "TIMELINE_ATOMIC_BATCH_FAILED")
    if receipt.get("operation_type") == "undo":
        if not receipt.get("restore_checkpoint_ref") or receipt.get("restore_checkpoint_committed") is not True:
            return ValidationResult(False, "TIMELINE_UNDO_CHECKPOINT_INVALID")
    if not receipt.get("parameters_digest"):
        return ValidationResult(False, "TIMELINE_PARAMETERS_DIGEST_REQUIRED")
    if not receipt.get("undo_ref"):
        return ValidationResult(False, "TIMELINE_UNDO_REF_REQUIRED")
    if not receipt.get("preview_evidence_ref"):
        return ValidationResult(False, "TIMELINE_PREVIEW_EVIDENCE_REQUIRED")
    if not receipt.get("timeline_digest_after"):
        return ValidationResult(False, "TIMELINE_DIGEST_REQUIRED")
    return ValidationResult(True, "TIMELINE_RECEIPT_ACCEPTED", returned_version=after)


def validate_release_timeline_binding(
    decision: Mapping[str, Any], *, current_version: int, current_digest: str
) -> ValidationResult:
    if decision.get("approved_timeline_version") != current_version:
        return ValidationResult(False, "RELEASE_DECISION_STALE_TIMELINE")
    if decision.get("approved_timeline_digest") != current_digest:
        return ValidationResult(False, "RELEASE_DECISION_STALE_TIMELINE")
    return ValidationResult(True, "RELEASE_TIMELINE_BINDING_VALID")
