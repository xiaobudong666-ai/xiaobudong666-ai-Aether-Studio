"""M1-C1 runtime gate wiring (fail-closed) for the talking-head release path.

These are thin enforcement adapters around the already-merged pure evaluators
(:mod:`app.timeline_receipt_validator`, :mod:`app.quality_release_gate`,
:mod:`app.render_parity_contract`, :mod:`app.platform_rule_evaluator`).

They do not perform I/O, do not mutate state, do not retry/fallback, and never
call a Provider. A failure raises :class:`fastapi.HTTPException` so the caller
(candidate adoption / release) aborts before any adoption or release record is
written, while the produced artifact and candidate remain untouched.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from fastapi import HTTPException

from .platform_rule_evaluator import evaluate_rule_pack
from .quality_release_gate import evaluate_quality_release, validate_human_release_decision
from .render_parity_contract import evaluate_render_parity
from .timeline_receipt_validator import (
    validate_release_timeline_binding,
    validate_timeline_receipt,
)

GATE_HTTP_STATUS = 422


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deny(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=GATE_HTTP_STATUS,
        detail={"code": code, "message": message},
    )


def enforce_timeline_receipt(
    receipt: Mapping[str, Any] | None,
    *,
    current_version: int,
) -> None:
    """Validate a client-attested timeline receipt before applying a draft.

    The receipt binds the apply operation to the exact prior project version
    (CAS) and attests atomicity/idempotency plus preview evidence. Missing or
    conflicting receipts fail closed.
    """
    if not receipt:
        raise _deny(
            "TIMELINE_RECEIPT_REQUIRED",
            "受治理路径要求提供时间线回执",
        )
    result = validate_timeline_receipt(
        receipt,
        current_version=current_version,
        prior_commit=None,
    )
    if not result.accepted:
        raise _deny(
            result.code,
            "时间线回执未通过 M1-C1 校验",
        )


_RULE_PACK_DATETIME_KEYS = ("revalidate_after", "effective_from", "effective_to")


def _parse_rule_pack_datetime(value: Any) -> Any:
    """Adapt JSON ISO-8601 strings to tz-aware datetimes for the pure evaluator."""
    if not isinstance(value, str):
        return value
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def enforce_release_gates(
    *,
    release_decision: Mapping[str, Any] | None,
    quality_findings: list[Mapping[str, Any]] | None,
    parity_preview: Mapping[str, Any] | None,
    parity_final: Mapping[str, Any] | None,
    parity_policy_version: str | None,
    rule_pack: Mapping[str, Any] | None,
    rule_content: Mapping[str, Any] | None,
    timeline_version: int,
    timeline_digest: str,
    preview_evidence_ref: str,
    rights_evidence_digest: str,
    evaluation_time: Any,
) -> None:
    """Enforce the full M1-C1 release gate set before adoption.

    Every gate must pass; any failure aborts adoption/release without deleting
    the rendered artifact, retrying, falling back, or publishing.
    """
    decision = release_decision or {}

    binding = validate_release_timeline_binding(
        decision,
        current_version=timeline_version,
        current_digest=timeline_digest,
    )
    if not binding.accepted:
        raise _deny(binding.code, "发布决策未绑定当前时间线，已阻断采用")

    quality = evaluate_quality_release(quality_findings or [])
    if not quality.eligible_for_human_release:
        raise _deny(quality.code, "质量发布门禁未通过，已阻断采用")

    human = validate_human_release_decision(
        decision,
        timeline_digest=timeline_digest,
        preview_evidence_ref=preview_evidence_ref,
        qa_summary_digest=_sha256_json(quality_findings or []),
        rule_pack_digest=_sha256_json(rule_pack or {}),
        rights_evidence_digest=rights_evidence_digest,
        parity_policy_version=parity_policy_version or "",
    )
    if not human.eligible_for_human_release:
        raise _deny(human.code, "人工发布决策未通过校验，已阻断采用")

    parity = evaluate_render_parity(
        parity_preview or {},
        parity_final or {},
        approved_threshold_version=parity_policy_version or "",
    )
    if not parity.valid:
        raise _deny(parity.code, "渲染一致性校验未通过，已阻断采用")

    normalized_pack = {
        key: (
            _parse_rule_pack_datetime(value)
            if key in _RULE_PACK_DATETIME_KEYS
            else value
        )
        for key, value in (rule_pack or {}).items()
    }
    rules = evaluate_rule_pack(
        normalized_pack,
        evaluation_time=evaluation_time,
        content=rule_content or {},
    )
    if not rules.allowed_to_prepare:
        raise _deny(rules.code, "平台规则评估未通过，已阻断采用")
