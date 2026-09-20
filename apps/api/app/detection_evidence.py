"""Deterministic P0 detection/evidence producer.

Pure/fake-only evidence normalization. No Provider, network, database mutation,
automatic approval, or publication side effects.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

SUPPORTED_ASPECT = "9:16"
SUPPORTED_WIDTH = 1080
SUPPORTED_HEIGHT = 1920
MAX_P0_DURATION_SECONDS = 10.0
PARITY_POLICY_VERSION = "p0-v1"


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finding(code: str, state: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "state": state, "evidence": evidence}


def media_quality_findings(probe: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Produce deterministic technical findings for a P0 talking-head artifact."""
    findings: list[dict[str, Any]] = []
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    aspect = str(probe.get("aspect") or "")
    duration = float(probe.get("durationSeconds") or 0)
    has_video = bool(probe.get("hasVideo"))
    has_audio = bool(probe.get("hasAudio"))
    subtitle_count = int(probe.get("subtitleCueCount") or 0)

    findings.append(_finding("MEDIA_VIDEO_PRESENT", "PASS" if has_video else "BLOCK_NON_OVERRIDABLE", hasVideo=has_video))
    findings.append(_finding(
        "MEDIA_CANVAS_9_16",
        "PASS" if (width, height, aspect) == (SUPPORTED_WIDTH, SUPPORTED_HEIGHT, SUPPORTED_ASPECT) else "BLOCK_NON_OVERRIDABLE",
        width=width, height=height, aspect=aspect,
    ))
    findings.append(_finding(
        "MEDIA_DURATION_P0",
        "PASS" if 0 < duration <= MAX_P0_DURATION_SECONDS else "BLOCK_NON_OVERRIDABLE",
        durationSeconds=duration, maxSeconds=MAX_P0_DURATION_SECONDS,
    ))
    findings.append(_finding("MEDIA_AUDIO_PRESENT", "PASS" if has_audio else "REVIEW_REQUIRED", hasAudio=has_audio))
    findings.append(_finding("MEDIA_SUBTITLE_CUES", "PASS" if subtitle_count > 0 else "REVIEW_REQUIRED", subtitleCueCount=subtitle_count))
    return findings


def talking_head_quality_findings(metrics: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize optional talking-head detectors without inventing PASS evidence."""
    specs = (
        ("faceStable", "TALKING_HEAD_FACE_STABILITY"),
        ("lipSyncPass", "TALKING_HEAD_LIP_SYNC"),
        ("deformationPass", "TALKING_HEAD_DEFORMATION"),
        ("motionStable", "TALKING_HEAD_MOTION"),
        ("avDurationAligned", "TALKING_HEAD_AV_ALIGNMENT"),
    )
    findings: list[dict[str, Any]] = []
    for key, code in specs:
        value = metrics.get(key)
        if value is True:
            state = "PASS"
        elif value is False:
            state = "BLOCK_NON_OVERRIDABLE"
        else:
            state = "REVIEW_REQUIRED"
        findings.append(_finding(code, state, observed=value))
    return findings


def normalize_rights_evidence(*, media_ids: Sequence[str], rights: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized = sorted(
        (
            {
                "mediaId": str(item.get("mediaId") or ""),
                "status": str(item.get("status") or "MISSING"),
                "purpose": str(item.get("purpose") or "EXPORT"),
                "territory": str(item.get("territory") or ""),
                "evidenceRef": str(item.get("evidenceRef") or ""),
            }
            for item in rights
        ),
        key=lambda item: item["mediaId"],
    )
    expected = sorted(str(value) for value in media_ids)
    observed = [item["mediaId"] for item in normalized]
    allowed = observed == expected and all(
        item["status"] == "ALLOWED" and item["purpose"] == "EXPORT" and item["evidenceRef"]
        for item in normalized
    )
    return {
        "mediaIds": expected,
        "purpose": "EXPORT",
        "status": "ALLOWED" if allowed else "BLOCKED",
        "items": normalized,
        "digest": _digest({"mediaIds": expected, "items": normalized}),
    }


def _normalize_render_parity_final(
    parity_base: Mapping[str, Any],
    final_evidence_ref: str,
    render_parity: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize caller-supplied parity evidence only.

    Missing evidence must never look like PASS. Only an explicit, caller-provided
    detector/validator result (structural_match=True plus a passing perceptual
    result) may produce PASS-shaped parity evidence.
    """
    final: dict[str, Any] = {
        **parity_base,
        "evidence_ref": final_evidence_ref,
        "threshold_set_version": PARITY_POLICY_VERSION,
    }
    structural = render_parity.get("structural_match") if render_parity else None
    perceptual = render_parity.get("perceptual_result") if render_parity else None

    if structural is True and perceptual in ("pass", "encoding_only_variance"):
        final["structural_match"] = True
        final["perceptual_result"] = perceptual
        return final

    # Fail-closed: missing, ambiguous, or failing evidence is never PASS-shaped.
    final["structural_match"] = True if structural is True else None
    final["perceptual_result"] = (
        perceptual
        if perceptual not in (None, "pass", "encoding_only_variance")
        else "review_required"
    )
    return final


def build_release_evidence(
    *,
    timeline_version: int,
    timeline: Mapping[str, Any],
    preview_evidence_ref: str,
    final_evidence_ref: str,
    media_probe: Mapping[str, Any],
    talking_head_metrics: Mapping[str, Any],
    rights_evidence: Mapping[str, Any],
    rule_pack: Mapping[str, Any],
    rule_content: Mapping[str, Any],
    source_digest: str,
    caption_digest: str,
    audio_digest: str,
    render_parity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build machine evidence only. Human releaseDecision is intentionally absent.

    Preview/Final parity evidence is never invented here: only caller-supplied
    detector/validator parity evidence is normalized into the final parity shape.
    """
    timeline_digest = _digest(timeline)
    quality = media_quality_findings(media_probe) + talking_head_quality_findings(talking_head_metrics)
    parity_base = {
        "timeline_version": timeline_version,
        "timeline_digest": timeline_digest,
        "source_digest": source_digest,
        "caption_digest": caption_digest,
        "audio_digest": audio_digest,
    }
    return {
        "qualityFindings": quality,
        "renderParityPreview": {**parity_base, "evidence_ref": preview_evidence_ref},
        "renderParityFinal": _normalize_render_parity_final(
            parity_base, final_evidence_ref, render_parity
        ),
        "renderParityPolicyVersion": PARITY_POLICY_VERSION,
        "rulePack": dict(rule_pack),
        "ruleContent": dict(rule_content),
        "rightsEvidence": dict(rights_evidence),
        "rightsEvidenceDigest": _digest({
            "mediaIds": rights_evidence.get("mediaIds", []),
            "purpose": rights_evidence.get("purpose"),
            "status": rights_evidence.get("status"),
        }),
        "previewEvidenceRef": preview_evidence_ref,
        "timelineDigest": timeline_digest,
        "releaseDecision": None,
    }
