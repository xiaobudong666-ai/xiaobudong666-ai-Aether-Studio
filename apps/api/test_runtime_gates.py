"""G1: M1-C1 runtime gate wiring tests (governance ON is fail-closed).

Fake-only, no Provider, no network. Covers the pure gate adapters plus the two
governed endpoints (apply-talking-head-draft and candidate adoption).
"""
import datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.generation_tasks import sha256_json
from app.main import create_app
from app.models import DBAdoption, DBCandidate, DBMasterRevision
from app.runtime_gates import enforce_release_gates, enforce_timeline_receipt

from test_generation_tasks import (
    OWNER_PASSWORD as GEN_OWNER_PASSWORD,
    WORKER_TOKEN as GEN_WORKER_TOKEN,
    FakeGeneratedMediaStore,
    claim,
    create_project as create_generation_project,
    create_task,
    ingest_success,
    transition,
)
from test_main import (
    OWNER_PASSWORD as RENDER_OWNER_PASSWORD,
    WORKER_TOKEN as RENDER_WORKER_TOKEN,
    FakeVideoUseAdapter,
    _complete_render_candidate,
    create_project as create_render_project,
)

DIGEST = "sha256:" + "a" * 64


# ---------------------------------------------------------------- pure adapters
def valid_receipt(before, after, key="apply-receipt-0001"):
    return {
        "idempotency_key": key,
        "timeline_version_before": before,
        "timeline_version_after": after,
        "timeline_digest_after": DIGEST,
        "operation_type": "trim",
        "parameters_digest": DIGEST,
        "undo_ref": "cp1",
        "preview_evidence_ref": "pv1",
    }


def test_enforce_timeline_receipt_missing_fails_closed():
    with pytest.raises(HTTPException) as exc:
        enforce_timeline_receipt(None, current_version=1)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "TIMELINE_RECEIPT_REQUIRED"


def test_enforce_timeline_receipt_conflict_fails_closed():
    with pytest.raises(HTTPException) as exc:
        enforce_timeline_receipt(valid_receipt(0, 1), current_version=1)
    assert exc.value.detail["code"] == "TIMELINE_VERSION_CONFLICT"


def test_enforce_timeline_receipt_valid_passes():
    enforce_timeline_receipt(valid_receipt(1, 2), current_version=1)


def _release_args(client_project_timeline, version, preview_ref, media_id):
    quality_findings = [{"state": "PASS"}]
    now = datetime.datetime.now(datetime.timezone.utc)
    rule_pack = {
        "verification_status": "verified",
        "source_fingerprint": DIGEST,
        "revalidate_after": (now + datetime.timedelta(days=1)).isoformat(),
        "effective_from": (now - datetime.timedelta(days=1)).isoformat(),
        "platform_overlay_status": "verified",
    }
    rights_evidence = {"mediaIds": [media_id], "purpose": "EXPORT", "status": "ALLOWED"}
    timeline_digest = sha256_json(client_project_timeline)
    parity_base = {
        "timeline_version": version,
        "timeline_digest": timeline_digest,
        "source_digest": DIGEST,
        "caption_digest": DIGEST,
        "audio_digest": DIGEST,
    }
    decision = {
        "actor_type": "human",
        "decision": "APPROVE",
        "approved_timeline_version": version,
        "approved_timeline_digest": timeline_digest,
        "timeline_digest": timeline_digest,
        "preview_evidence_ref": preview_ref,
        "qa_summary_digest": sha256_json(quality_findings),
        "rule_pack_digest": sha256_json(rule_pack),
        "rights_evidence_digest": sha256_json(rights_evidence),
        "parity_policy_version": "v1",
    }
    return dict(
        release_decision=decision,
        quality_findings=quality_findings,
        parity_preview={**parity_base, "evidence_ref": "pv1"},
        parity_final={
            **parity_base,
            "evidence_ref": "final1",
            "threshold_set_version": "v1",
            "structural_match": True,
            "perceptual_result": "pass",
        },
        parity_policy_version="v1",
        rule_pack=rule_pack,
        rule_content={},
        timeline_version=version,
        timeline_digest=timeline_digest,
        preview_evidence_ref=preview_ref,
        rights_evidence_digest=sha256_json(rights_evidence),
        evaluation_time=now,
    )


def test_enforce_release_gates_missing_evidence_fails_closed():
    with pytest.raises(HTTPException) as exc:
        enforce_release_gates(
            release_decision=None, quality_findings=None,
            parity_preview=None, parity_final=None,
            parity_policy_version=None, rule_pack=None, rule_content=None,
            timeline_version=1, timeline_digest=DIGEST,
            preview_evidence_ref="pv", rights_evidence_digest=DIGEST,
            evaluation_time=datetime.datetime.now(datetime.timezone.utc),
        )
    assert exc.value.detail["code"] == "RELEASE_DECISION_STALE_TIMELINE"


def test_enforce_release_gates_full_valid_passes():
    args = _release_args({"tracks": []}, 3, "/api/renders/t/artifact", "media-1")
    enforce_release_gates(**args)


def test_enforce_release_gates_stale_decision_fails_closed():
    args = _release_args({"tracks": []}, 3, "/api/renders/t/artifact", "media-1")
    args["release_decision"] = dict(args["release_decision"], approved_timeline_version=2)
    with pytest.raises(HTTPException) as exc:
        enforce_release_gates(**args)
    assert exc.value.detail["code"] == "RELEASE_DECISION_STALE_TIMELINE"


# ------------------------------------------------------------------ fixtures
@pytest.fixture()
def governed_generation_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_MAX_UPLOAD_BYTES", "1024")
    engine = build_engine(f"sqlite:///{tmp_path / 'g1-generation.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app = create_app(
        app_engine=engine,
        db_dependency=get_db,
        video_use_adapter=FakeGeneratedMediaStore(),
        bootstrap_admin_password=GEN_OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        worker_token=GEN_WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
        governance_gates=True,
    )
    with TestClient(app) as client:
        assert client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": GEN_OWNER_PASSWORD},
        ).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, sessions
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def governed_render_context(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'g1-render.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app = create_app(
        app_engine=engine,
        db_dependency=get_db,
        video_use_adapter=FakeVideoUseAdapter(),
        bootstrap_admin_password=RENDER_OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        worker_token=RENDER_WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        governance_gates=True,
    )
    with TestClient(app) as client:
        assert client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": RENDER_OWNER_PASSWORD},
        ).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, sessions
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ----------------------------------------------------------- apply-draft gate
def test_apply_draft_receipt_gate_fails_closed_and_passes(governed_generation_context):
    client, sessions = governed_generation_context
    project = create_generation_project(client, "G1 apply gate")
    task = create_task(client, project, key="g1-apply-gate")
    claimed = claim(client).json()
    assert claimed["taskId"] == task["taskId"]
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream-1").status_code == 200
    assert transition(
        client, task["taskId"], "INGESTING", progress=95,
        upstreamJobId="upstream-1", providerArtifactId="artifact-1",
    ).status_code == 200
    result = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": GEN_WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact-1"},
        files={"file": ("artifact.mp4", b"deterministic-video", "video/mp4")},
    ).json()
    rights = client.post(
        f"/projects/{project['id']}/asset-versions/{result['results'][0]['assetVersionId']}/rights-snapshots",
        json={"status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL"},
    )
    assert rights.status_code == 201

    current = client.get(f"/projects/{project['id']}").json()
    endpoint = f"/projects/{project['id']}/generation-tasks/{task['taskId']}/apply-talking-head-draft"

    missing = client.post(endpoint, json={"expectedRevision": current["revision"], "aspect": "9:16", "subtitles": []})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "TIMELINE_RECEIPT_REQUIRED"
    assert client.get(f"/projects/{project['id']}").json()["revision"] == current["revision"]

    applied = client.post(
        endpoint,
        json={
            "expectedRevision": current["revision"],
            "aspect": "9:16",
            "subtitles": [],
            "timelineReceipt": valid_receipt(current["revision"], current["revision"] + 1),
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["revision"] == current["revision"] + 1


# -------------------------------------------------------------- adopt gate
def test_adopt_release_gate_fails_closed_and_passes(governed_render_context):
    client, sessions = governed_render_context
    project = create_render_project(client, "G1 adopt gate")
    upload, candidate = _complete_render_candidate(client, project)
    media_id = upload["material"]["id"]
    asset_id = upload["assetVersion"]["id"]

    now = datetime.datetime.now(datetime.timezone.utc)
    assert client.post(
        f"/projects/{project['id']}/asset-versions/{asset_id}/rights-snapshots",
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
        },
    ).status_code == 201

    proj = client.get(f"/projects/{project['id']}").json()
    timeline_digest = sha256_json(proj["timeline"])
    version = proj["revision"]
    preview_ref = candidate["artifactRef"]

    endpoint = f"/projects/{project['id']}/candidates/{candidate['id']}/adopt"

    blocked = client.post(
        endpoint,
        headers={"Idempotency-Key": "g1-adopt-blocked"},
        json={"reason": "missing gate evidence"},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "RELEASE_DECISION_STALE_TIMELINE"

    with sessions() as db:
        assert db.execute(select(func.count(DBAdoption.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBMasterRevision.id))).scalar_one() == 0
        assert db.get(DBCandidate, candidate["id"]).status == "READY"

    quality_findings = [{"state": "PASS"}]
    rule_pack = {
        "verification_status": "verified",
        "source_fingerprint": DIGEST,
        "revalidate_after": (now + datetime.timedelta(days=1)).isoformat(),
        "effective_from": (now - datetime.timedelta(days=1)).isoformat(),
        "platform_overlay_status": "verified",
    }
    rights_evidence = {"mediaIds": [media_id], "purpose": "EXPORT", "status": "ALLOWED"}
    parity_base = {
        "timeline_version": version,
        "timeline_digest": timeline_digest,
        "source_digest": DIGEST,
        "caption_digest": DIGEST,
        "audio_digest": DIGEST,
    }
    decision = {
        "actor_type": "human",
        "decision": "APPROVE",
        "approved_timeline_version": version,
        "approved_timeline_digest": timeline_digest,
        "timeline_digest": timeline_digest,
        "preview_evidence_ref": preview_ref,
        "qa_summary_digest": sha256_json(quality_findings),
        "rule_pack_digest": sha256_json(rule_pack),
        "rights_evidence_digest": sha256_json(rights_evidence),
        "parity_policy_version": "v1",
    }
    adopted = client.post(
        endpoint,
        headers={"Idempotency-Key": "g1-adopt-valid"},
        json={
            "reason": "Owner approved release",
            "releaseDecision": decision,
            "qualityFindings": quality_findings,
            "renderParityPreview": {**parity_base, "evidence_ref": "pv1"},
            "renderParityFinal": {
                **parity_base,
                "evidence_ref": "final1",
                "threshold_set_version": "v1",
                "structural_match": True,
                "perceptual_result": "pass",
            },
            "renderParityPolicyVersion": "v1",
            "rulePack": rule_pack,
            "ruleContent": {},
        },
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["revisionNo"] == 1

    with sessions() as db:
        assert db.execute(select(func.count(DBAdoption.id))).scalar_one() == 1
        assert db.get(DBCandidate, candidate["id"]).status == "ADOPTED"
