from app.detection_evidence import (
    build_release_evidence,
    media_quality_findings,
    normalize_rights_evidence,
    talking_head_quality_findings,
)


def _probe(**overrides):
    value = {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "durationSeconds": 8.0, "hasVideo": True, "hasAudio": True,
        "subtitleCueCount": 2,
    }
    value.update(overrides)
    return value


def test_media_quality_passes_bounded_vertical_artifact():
    findings = media_quality_findings(_probe())
    assert {item["state"] for item in findings} == {"PASS"}


def test_media_quality_fails_closed_for_wrong_canvas_and_duration():
    findings = media_quality_findings(_probe(width=1920, height=1080, aspect="16:9", durationSeconds=11))
    by_code = {item["code"]: item["state"] for item in findings}
    assert by_code["MEDIA_CANVAS_9_16"] == "BLOCK_NON_OVERRIDABLE"
    assert by_code["MEDIA_DURATION_P0"] == "BLOCK_NON_OVERRIDABLE"


def test_missing_talking_head_detector_never_invents_pass():
    findings = talking_head_quality_findings({})
    assert {item["state"] for item in findings} == {"REVIEW_REQUIRED"}


def test_talking_head_explicit_failure_is_non_overridable():
    findings = talking_head_quality_findings({"lipSyncPass": False})
    assert next(item for item in findings if item["code"] == "TALKING_HEAD_LIP_SYNC")["state"] == "BLOCK_NON_OVERRIDABLE"


def test_rights_evidence_requires_complete_allowed_export_evidence():
    evidence = normalize_rights_evidence(
        media_ids=["m1"],
        rights=[{"mediaId": "m1", "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL", "evidenceRef": "ev://1"}],
    )
    assert evidence["status"] == "ALLOWED"
    blocked = normalize_rights_evidence(media_ids=["m1"], rights=[])
    assert blocked["status"] == "BLOCKED"


def test_release_evidence_never_auto_approves_human_release():
    evidence = build_release_evidence(
        timeline_version=4,
        timeline={"version": "1.1", "tracks": []},
        preview_evidence_ref="preview://1",
        final_evidence_ref="final://1",
        media_probe=_probe(),
        talking_head_metrics={
            "faceStable": True, "lipSyncPass": True, "deformationPass": True,
            "motionStable": True, "avDurationAligned": True,
        },
        rights_evidence={"mediaIds": ["m1"], "purpose": "EXPORT", "status": "ALLOWED"},
        rule_pack={"verification_status": "verified"},
        rule_content={"ai_generated_or_synthetic": True},
        source_digest="source",
        caption_digest="caption",
        audio_digest="audio",
    )
    assert evidence["releaseDecision"] is None
    assert evidence["renderParityFinal"]["perceptual_result"] == "pass"
    assert evidence["rightsEvidenceDigest"]
    assert evidence["timelineDigest"]


def test_review_required_quality_prevents_machine_parity_pass():
    evidence = build_release_evidence(
        timeline_version=1, timeline={"tracks": []},
        preview_evidence_ref="preview://1", final_evidence_ref="final://1",
        media_probe=_probe(subtitleCueCount=0),
        talking_head_metrics={},
        rights_evidence={"mediaIds": [], "purpose": "EXPORT", "status": "BLOCKED"},
        rule_pack={}, rule_content={},
        source_digest="s", caption_digest="c", audio_digest="a",
    )
    assert evidence["renderParityFinal"]["perceptual_result"] == "review_required"
    assert evidence["releaseDecision"] is None
