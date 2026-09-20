from test_runtime_gates import governed_generation_context
from test_generation_tasks import create_project as create_generation_project

from app.detection_evidence import (
    PARITY_POLICY_VERSION,
    build_release_evidence,
    media_quality_findings,
    normalize_rights_evidence,
    talking_head_quality_findings,
)
from app.render_parity_contract import evaluate_render_parity


def _probe(**overrides):
    value = {
        "width": 1080, "height": 1920, "aspect": "9:16",
        "durationSeconds": 8.0, "hasVideo": True, "hasAudio": True,
        "subtitleCueCount": 2,
    }
    value.update(overrides)
    return value


def _build(**overrides):
    kwargs = dict(
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
    kwargs.update(overrides)
    return build_release_evidence(**kwargs)


def _parity_is_pass(evidence):
    return evaluate_render_parity(
        evidence["renderParityPreview"],
        evidence["renderParityFinal"],
        approved_threshold_version=PARITY_POLICY_VERSION,
    ).valid


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


def test_missing_parity_evidence_never_passes():
    evidence = _build()
    final = evidence["renderParityFinal"]
    assert final["structural_match"] is not True
    assert final["perceptual_result"] != "pass"
    assert _parity_is_pass(evidence) is False


def test_explicit_failing_parity_fails_closed():
    evidence = _build(render_parity={"structural_match": False, "perceptual_result": "fail"})
    final = evidence["renderParityFinal"]
    assert final["structural_match"] is not True
    assert final["perceptual_result"] != "pass"
    assert _parity_is_pass(evidence) is False


def test_explicit_valid_parity_enters_pass_evidence():
    evidence = _build(render_parity={"structural_match": True, "perceptual_result": "pass"})
    final = evidence["renderParityFinal"]
    assert final["structural_match"] is True
    assert final["perceptual_result"] == "pass"
    assert _parity_is_pass(evidence) is True


def test_human_release_decision_never_machine_generated():
    evidence = _build()
    assert evidence["releaseDecision"] is None
    assert "releaseDecision" in evidence


def test_detection_endpoint_requires_current_revision_and_never_approves(governed_generation_context):
    client, _ = governed_generation_context
    project = create_generation_project(client, "D1 evidence")
    endpoint = f"/projects/{project['id']}/detection-evidence"
    payload = {
        "timelineVersion": project["revision"],
        "previewEvidenceRef": "preview://d1",
        "finalEvidenceRef": "final://d1",
        "mediaProbe": _probe(),
        "talkingHeadMetrics": {},
        "mediaIds": [],
        "rightsEvidence": [],
        "rulePack": {"verification_status": "verified"},
        "ruleContent": {"ai_generated_or_synthetic": True},
        "sourceDigest": "source",
        "captionDigest": "caption",
        "audioDigest": "audio",
    }
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence["releaseDecision"] is None
    assert any(item["state"] == "REVIEW_REQUIRED" for item in evidence["qualityFindings"])
    # Missing caller-supplied parity evidence must not be reported as PASS.
    assert evidence["renderParityFinal"]["structural_match"] is not True
    assert evidence["renderParityFinal"]["perceptual_result"] != "pass"

    payload["timelineVersion"] = project["revision"] + 1
    stale = client.post(endpoint, json=payload)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"
