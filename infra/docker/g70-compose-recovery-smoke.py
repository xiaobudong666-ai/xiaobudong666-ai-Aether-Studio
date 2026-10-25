"""FAKE-ONLY G70 Compose API -> video-use original media readback.
Synthetic SQL identity/REMIX rights and FFmpeg bytes, not M11/M12 evidence.
No business asset relinking, paid generation, refund, release, or Provider calls.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0, "/app")
import httpx
from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import (DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
                        DBGenerationTask, DBGenerationUsageEntry, DBProject,
                        DBRightsSnapshot, DBTenant)
from app.video_use_adapter import VideoUseAdapter, VideoUseError

STATE = Path("/tmp/g70-compose-original-state.json")
VIDEO = Path("/tmp/g70-ci-synthetic-original.mp4")
BASE = "http://127.0.0.1:8000"
SIDECAR = "http://video-use:8002"

def gates():
    assert os.getenv("CI") == "true", "CI-only; refuse arbitrary environments"
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    password = os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "")
    assert password.startswith("ci-") and len(password) > 20
    assert not os.environ.get("HEYGEN_API_KEY")
    return password

@contextmanager
def login():
    # A client used for authentication may not be entered a second time.
    with httpx.Client(base_url=BASE, timeout=30, trust_env=False) as client:
        response = client.post("/auth/login", json={
            "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
            "password": gates()})
        assert response.status_code == 200, f"login={response.status_code}"
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client

def invariant(task_id, tenant_id):
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        assert task is not None and tenant is not None
        def count(model, criterion):
            return db.execute(select(func.count(model.id)).where(criterion)).scalar_one()
        return {
            "status": task.status, "error": task.error_code,
            "businessAssetId": task.asset_version_id, "mediaId": task.media_id,
            "attempts": count(DBGenerationAttempt,
                              DBGenerationAttempt.generation_task_id == task_id),
            "events": count(DBGenerationEvent,
                            DBGenerationEvent.generation_task_id == task_id),
            "videoAssets": count(DBAssetVersion,
                                 (DBAssetVersion.project_id == task.project_id)
                                 & (DBAssetVersion.media_type == "video")),
            "settlement": count(DBGenerationUsageEntry,
                                (DBGenerationUsageEntry.task_id == task_id)
                                & (DBGenerationUsageEntry.kind == "SETTLED")),
            "storageHeld": tenant.used_storage_bytes}

def prepare():
    original = VIDEO.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    assert len(original) > 900 and original[4:8] == b"ftyp"
    with login() as client:
        created = client.post("/projects", json={"name": "G70 disposable " + uuid.uuid4().hex[:8]})
        assert created.status_code == 201, created.text[:500]
        pid = created.json()["id"]
        other = client.post("/projects", json={"name": "G70 unrelated " + uuid.uuid4().hex[:8]})
        assert other.status_code == 201
        other_id = other.json()["id"]
        now = dt.datetime.now(dt.timezone.utc)
        task_id, attempt_id, image_id = [str(uuid.uuid4()) for _ in range(3)]
        upstream, artifact = "synthetic-original-job", "synthetic-original-output"
        with SessionLocal() as db:
            project = db.get(DBProject, pid)
            tenant_id = project.tenant_id
            key = "g70-" + hashlib.sha256(
                f"{tenant_id}:{pid}:{task_id}:1:{artifact}".encode()).hexdigest()
            source = DBAssetVersion(
                id=image_id, tenant_id=tenant_id, project_id=pid, version_no=1,
                media_id="synthetic-source-" + uuid.uuid4().hex,
                sha256="a" * 64, media_type="image", content_type="image/png",
                size_bytes=100, probe_json={"image":{"width":64,"height":64}},
                created_by=project.owner_id, created_at=now)
            rights = DBRightsSnapshot(
                id=str(uuid.uuid4()), tenant_id=tenant_id, project_id=pid,
                asset_version_id=image_id, status="ALLOWED", purpose="REMIX",
                territory="TEST", valid_from=None, valid_until=None,
                evidence_ref="SYNTHETIC_FIXTURE_NOT_M12",
                captured_by=project.owner_id, captured_at=now)
            task = DBGenerationTask(
                id=task_id, tenant_id=tenant_id, project_id=pid,
                requested_by=project.owner_id, provider="deterministic-fake-talking-head",
                status="UNKNOWN", progress=90, message="Synthetic ambiguous media ACK",
                request_json={"imageAssetId":image_id, "sourceAssetSha256":"a"*64},
                request_hash="b"*64, capability_snapshot_json={},
                capability_snapshot_hash="c"*64, idempotency_key=str(uuid.uuid4()),
                upstream_job_id=upstream, provider_artifact_id=artifact,
                media_id=None, asset_version_id=None, attempts=1, max_attempts=1,
                lease_owner=None, lease_expires_at=None, cancel_requested_at=None,
                error_code="ARTIFACT_INGEST_FAILED", error_message="Synthetic ACK lost",
                created_at=now, updated_at=now, started_at=now, completed_at=now)
            attempt = DBGenerationAttempt(
                id=attempt_id, generation_task_id=task_id, attempt_no=1,
                status="UNKNOWN", submission_started_at=now,
                upstream_job_id=upstream, reconciliation_state="UNKNOWN",
                error_code="ARTIFACT_INGEST_FAILED", error_message="Synthetic ACK lost",
                created_at=now, completed_at=now)
            intent = DBGenerationEvent(
                id=str(uuid.uuid4()), generation_task_id=task_id, attempt_id=attempt_id,
                event_type="ARTIFACT_UPLOAD_INTENT",
                from_status="INGESTING", to_status="INGESTING",
                actor_type="WORKER", actor_id="synthetic-ci",
                metadata_json={"tenantId":tenant_id, "projectId":pid, "attempt":1,
                               "upstreamJobId":upstream, "providerArtifactId":artifact,
                               "sha256":sha, "sizeBytes":len(original),
                               "fakeRecoveryKey":key,
                               "recoveryState":"PREPARED_BEFORE_SIDECAR_UPLOAD"},
                created_at=now)
            tenant = db.get(DBTenant, tenant_id)
            assert tenant.used_storage_bytes == 0, "must be fresh disposable DB"
            tenant.used_storage_bytes += len(original)
            db.add_all([source, rights, task, attempt, intent])
            db.commit()   # Intent + held quota BEFORE external HTTP upload.
        adapter = VideoUseAdapter(api_url=SIDECAR, timeout=20, max_retries=0)
        uploaded = adapter.upload_media(
            pid, "synthetic-original.mp4", "video/mp4", io.BytesIO(original),
            recovery_key=key, expected_sha256=sha)
        assert uploaded["projectId"] == pid and uploaded["sha256"] == sha
        assert uploaded["mediaId"].startswith("g70")
        # ACK deliberately not propagated into business DB; no second POST.
        endpoint = f"/projects/{pid}/generation-tasks/{task_id}/fake-recovery-check"
        found = client.get(endpoint)
        assert found.status_code == 200, found.text[:500]
        body = found.json()
        assert body["recoveryState"] == "FOUND_UNBOUND"
        assert body["sha256"] == sha and body["mediaId"] == uploaded["mediaId"]
        assert body["sizeBytes"] == len(original)
        assert body["businessAssetBound"] is False
        assert body["financialSettlementVerified"] is False
        assert client.get(
            f"/projects/{other_id}/generation-tasks/{task_id}/fake-recovery-check"
        ).status_code == 404
        expected = {"status":"UNKNOWN", "error":"ARTIFACT_INGEST_FAILED",
                    "businessAssetId":None, "mediaId":None,
                    "attempts":1, "events":1, "videoAssets":0,
                    "settlement":0, "storageHeld":len(original)}
        assert invariant(task_id, tenant_id) == expected
        STATE.write_text(json.dumps({
            "taskId":task_id,"tenantId":tenant_id,"projectId":pid,
            "otherProjectId":other_id,"mediaId":uploaded["mediaId"],"sourceId":image_id,
            "key":key,"sha":sha,"size":len(original),"expected":expected}),encoding="utf8")
        print(json.dumps({"phase":"before_restart","fakeOnly":True,
            "fixture":"SYNTHETIC_DB_AND_FFMPEG","apiReadback":"FOUND_UNBOUND",
            "sha256":sha}),flush=True)

def verify():
    gates()
    state = json.loads(STATE.read_text(encoding="utf8"))
    adapter = VideoUseAdapter(api_url=SIDECAR, timeout=20, max_retries=0)
    original = adapter.find_fake_recovery(
        state["projectId"],state["key"],state["sha"])
    assert original["mediaId"] == state["mediaId"]
    assert original["sha256"] == state["sha"] and original["recovered"] is True
    try:
        adapter.find_fake_recovery(state["otherProjectId"],state["key"],state["sha"])
        raise AssertionError("Cross-project recovery unexpectedly succeeded")
    except VideoUseError:
        pass
    with login() as client:
        url = f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-check"
        for _ in range(2):
            response = client.get(url)
            assert response.status_code == 200, response.text[:500]
            assert response.json()["mediaId"] == state["mediaId"]
            assert response.json()["recoveryState"] == "FOUND_UNBOUND"
        assert client.get(
            f"/projects/{state['otherProjectId']}/generation-tasks/{state['taskId']}/fake-recovery-check"
        ).status_code == 404
        retry = client.post(
            f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/retry")
        assert retry.status_code == 409 and retry.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
    data = httpx.get(
        f"{SIDECAR}/media/{state['projectId']}/{state['mediaId']}",
        timeout=20,trust_env=False)
    assert data.status_code == 200
    assert len(data.content) == state["size"]
    assert hashlib.sha256(data.content).hexdigest() == state["sha"]
    assert invariant(state["taskId"],state["tenantId"]) == state["expected"]
    # A disposable, newer DENIED rights snapshot must fence any further
    # owner-self recovery lookup before contacting sidecar.  This is a
    # synthetic revocation, NOT an M12 trusted issuer or production policy.
    with SessionLocal() as db:
        project = db.get(DBProject, state["projectId"])
        assert project is not None
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=state["tenantId"],
            project_id=state["projectId"], asset_version_id=state["sourceId"],
            status="DENIED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_REVOCATION_NOT_M12",
            captured_by=project.owner_id,
            captured_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
        ))
        db.commit()
    with login() as client:
        revoked = client.get(
            f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-check"
        )
        assert revoked.status_code == 409, revoked.text[:500]
        assert revoked.json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"
    assert invariant(state["taskId"],state["tenantId"]) == state["expected"]
    # Revocation fences business readback but never deletes original media.
    remaining = adapter.find_fake_recovery(state["projectId"], state["key"], state["sha"])
    assert remaining["mediaId"] == state["mediaId"] and remaining["sha256"] == state["sha"]

    # Separate synthetic task: prove the new FAKE-only owner-self relink path
    # against the real Compose sidecar after restart. The earlier task remains
    # unbound because its source rights were deliberately revoked above.
    original_bytes = VIDEO.read_bytes()
    relink_sha = hashlib.sha256(original_bytes).hexdigest()
    relink_size = len(original_bytes)
    relink_task_id, relink_attempt_id, relink_source_id = [str(uuid.uuid4()) for _ in range(3)]
    relink_provider_artifact = "synthetic-original-relink-output"
    relink_key = "g70-" + hashlib.sha256(
        f"{state['tenantId']}:{state['projectId']}:{relink_task_id}:1:{relink_provider_artifact}".encode()
    ).hexdigest()
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, state["projectId"])
        tenant = db.get(DBTenant, state["tenantId"])
        assert project is not None and tenant is not None
        source = DBAssetVersion(
            id=relink_source_id, tenant_id=state["tenantId"], project_id=state["projectId"],
            version_no=1, media_id="g70-relink-source-" + uuid.uuid4().hex,
            sha256="d" * 64, media_type="image", content_type="image/png",
            size_bytes=128, probe_json={"image":{"width":64,"height":64}},
            created_by=project.owner_id, created_at=now)
        rights = DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=state["tenantId"], project_id=state["projectId"],
            asset_version_id=relink_source_id, status="ALLOWED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_RELINK_FIXTURE_NOT_M12",
            captured_by=project.owner_id, captured_at=now)
        task = DBGenerationTask(
            id=relink_task_id, tenant_id=state["tenantId"], project_id=state["projectId"],
            requested_by=project.owner_id, provider="deterministic-fake-talking-head",
            status="UNKNOWN", progress=90, message="Synthetic ambiguous sidecar ACK",
            request_json={"imageAssetId":relink_source_id, "sourceAssetSha256":"d"*64},
            request_hash="e"*64, capability_snapshot_json={}, capability_snapshot_hash="f"*64,
            idempotency_key=str(uuid.uuid4()), upstream_job_id="synthetic-relink-job",
            provider_artifact_id=relink_provider_artifact, media_id=None, asset_version_id=None,
            attempts=1, max_attempts=1, lease_owner=None, lease_expires_at=None,
            cancel_requested_at=None, error_code="ARTIFACT_INGEST_FAILED",
            error_message="Synthetic ACK lost", created_at=now, updated_at=now,
            started_at=now, completed_at=now)
        attempt = DBGenerationAttempt(
            id=relink_attempt_id, generation_task_id=relink_task_id, attempt_no=1,
            status="UNKNOWN", submission_started_at=now, upstream_job_id=task.upstream_job_id,
            reconciliation_state="UNKNOWN", error_code="ARTIFACT_INGEST_FAILED",
            error_message="Synthetic ACK lost", created_at=now, completed_at=now)
        intent = DBGenerationEvent(
            id=str(uuid.uuid4()), generation_task_id=relink_task_id, attempt_id=relink_attempt_id,
            event_type="ARTIFACT_UPLOAD_INTENT", from_status="INGESTING", to_status="INGESTING",
            actor_type="WORKER", actor_id="synthetic-relink-ci",
            metadata_json={"tenantId":state["tenantId"], "projectId":state["projectId"],
                "attempt":1, "upstreamJobId":task.upstream_job_id,
                "providerArtifactId":relink_provider_artifact, "sha256":relink_sha,
                "sizeBytes":relink_size, "uploadName":f"generation-{relink_task_id}.mp4",
                "fakeRecoveryKey":relink_key, "recoveryState":"PREPARED_BEFORE_SIDECAR_UPLOAD"},
            created_at=now)
        reserved = DBGenerationUsageEntry(
            id=str(uuid.uuid4()), tenant_id=state["tenantId"], project_id=state["projectId"],
            task_id=relink_task_id, attempt_id=relink_attempt_id, kind="RESERVED",
            request_units=1, generated_seconds=0,
            reservation_key=f"generation:{relink_task_id}", config_version_id=None, created_at=now)
        tenant.used_storage_bytes += relink_size
        db.add_all([source, rights, task, attempt, intent, reserved])
        db.commit()

    uploaded = adapter.upload_media(
        state["projectId"], "synthetic-original-relink.mp4", "video/mp4",
        io.BytesIO(original_bytes), recovery_key=relink_key, expected_sha256=relink_sha)
    assert uploaded["projectId"] == state["projectId"]
    assert uploaded["sha256"] == relink_sha and uploaded["mediaId"].startswith("g70")
    relink_url = (
        f"/projects/{state['projectId']}/generation-tasks/{relink_task_id}/fake-recovery-relink"
    )
    with login() as client:
        first = client.post(relink_url)
        assert first.status_code == 200, first.text[:500]
        bound = first.json()
        assert bound["recoveryState"] == "BOUND_UNSETTLED"
        assert bound["actorMode"] == "OWNER_SELF"
        assert bound["businessAssetBound"] is True
        assert bound["financialSettlementVerified"] is False
        assert bound["settlementCreated"] is False
        assert bound["reservationPreserved"] is True
        assert bound["idempotent"] is False
        assert bound["mediaId"] == uploaded["mediaId"] and bound["sha256"] == relink_sha
        second = client.post(relink_url)
        assert second.status_code == 200, second.text[:500]
        replay = second.json()
        assert replay["idempotent"] is True
        assert replay["assetVersionId"] == bound["assetVersionId"]
        foreign = client.post(
            f"/projects/{state['otherProjectId']}/generation-tasks/{relink_task_id}/fake-recovery-relink")
        assert foreign.status_code == 404

    with SessionLocal() as db:
        task = db.get(DBGenerationTask, relink_task_id)
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == relink_task_id)).scalar_one()
        assets = db.execute(select(DBAssetVersion).where(
            DBAssetVersion.project_id == state["projectId"],
            DBAssetVersion.media_id == uploaded["mediaId"])).scalars().all()
        relinks = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == relink_task_id,
            DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED")).scalar_one()
        settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == relink_task_id,
            DBGenerationUsageEntry.kind == "SETTLED")).scalar_one()
        released = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == relink_task_id,
            DBGenerationUsageEntry.kind == "RELEASED")).scalar_one()
        reserved_count = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == relink_task_id,
            DBGenerationUsageEntry.kind == "RESERVED")).scalar_one()
        tenant = db.get(DBTenant, state["tenantId"])
        project = db.get(DBProject, state["projectId"])
        assert task.status == "UNKNOWN" and task.error_code == "ARTIFACT_RECOVERY_UNSETTLED"
        assert task.asset_version_id == bound["assetVersionId"] and task.media_id == uploaded["mediaId"]
        assert attempt.reconciliation_state == "ASSET_RELINKED_UNSETTLED"
        assert len(assets) == 1 and assets[0].sha256 == relink_sha
        assert assets[0].probe_json["provenance"]["generationTaskId"] == relink_task_id
        assert relinks == 1 and settled == 0 and released == 0 and reserved_count == 1
        assert tenant.used_storage_bytes == state["size"] + relink_size
        assert len([item for item in (project.materials or []) if item.get("id") == uploaded["mediaId"]]) == 1
    original_readback = httpx.get(
        f"{SIDECAR}/media/{state['projectId']}/{uploaded['mediaId']}",
        timeout=20, trust_env=False)
    assert original_readback.status_code == 200
    assert hashlib.sha256(original_readback.content).hexdigest() == relink_sha
    print(json.dumps({"phase":"asset_relinked","verified":True,"fixture":"REAL_COMPOSE_SIDECAR_AND_FFMPEG",
        "actorMode":"OWNER_SELF","assetRelink":True,"idempotentRetry":True,
        "settled":False,"released":False,"providerResubmitted":False,
        "sha256":relink_sha,"assetVersionId":bound["assetVersionId"]}),flush=True)
    print(json.dumps({"phase":"rights_revoked","synthetic":True,
        "ownerReadback":"DENIED","physicalMediaPreserved":True,
        "settled":False}),flush=True)
    print(json.dumps({"phase":"after_restart","verified":True,
        "fixture":"SYNTHETIC_DB_AND_FFMPEG","readback":"FOUND_UNBOUND",
        "assetRelink":False,"settled":False,"sha256":state["sha"]}),flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase",choices=("prepare","verify"))
    (prepare if parser.parse_args().phase == "prepare" else verify)()
