"""Pure Preview/Final parity evidence contract. No rendering is performed here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ParityResult:
    valid: bool
    code: str


def evaluate_render_parity(
    preview: Mapping[str, Any], final: Mapping[str, Any], *, approved_threshold_version: str
) -> ParityResult:
    required_equal = (
        "timeline_version",
        "timeline_digest",
        "source_digest",
        "caption_digest",
        "audio_digest",
    )
    for key in required_equal:
        if preview.get(key) != final.get(key):
            return ParityResult(False, "RENDER_PREVIEW_FINAL_MISMATCH")
    if final.get("threshold_set_version") != approved_threshold_version:
        return ParityResult(False, "RENDER_PARITY_POLICY_CHANGED")
    if not preview.get("evidence_ref") or not final.get("evidence_ref"):
        return ParityResult(False, "RENDER_PARITY_EVIDENCE_MISSING")
    if final.get("structural_match") is not True:
        return ParityResult(False, "RENDER_STRUCTURAL_MISMATCH")
    perceptual = final.get("perceptual_result")
    if perceptual not in {"pass", "encoding_only_variance"}:
        return ParityResult(False, "RENDER_PERCEPTUAL_REVIEW_REQUIRED")
    return ParityResult(True, "RENDER_PARITY_VALID")
