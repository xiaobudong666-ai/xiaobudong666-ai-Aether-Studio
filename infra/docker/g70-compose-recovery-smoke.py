"""FAKE-ONLY G70 Compose recovery for the same post-upload-ACK DB-failure task.
Synthetic SQL identity/REMIX rights and FFmpeg bytes, not M11/M12 evidence.
Owner-self relink is exercised; no paid generation, settlement, refund, release, or Provider calls.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from threading import Barrier
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import uuid
sys.path.insert(0, "/app")
import httpx
from sqlalchemy import func, select, text
from app.database import SessionLocal
from app.models import (DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
                        DBGenerationTask, DBGenerationUsageEntry, DBProject,
                        DBRightsSnapshot, DBTenant)
from app.video_use_adapter import VideoUseAdapter, VideoUseError

STATE = Path("/tmp/g70-compose-original-state.json")
STATE_REVOKE_RACE = Path("/tmp/g70-compose-revoke-race-state.json")
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
            "reserved": count(DBGenerationUsageEntry,
                              (DBGenerationUsageEntry.task_id == task_id)
                              & (DBGenerationUsageEntry.kind == "RESERVED")),
            "released": count(DBGenerationUsageEntry,
                              (DBGenerationUsageEntry.task_id == task_id)
                              & (DBGenerationUsageEntry.kind == "RELEASED")),
            "relinked": count(DBGenerationEvent,
                              (DBGenerationEvent.generation_task_id == task_id)
                              & (DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED")),
            "storageHeld": tenant.used_storage_bytes}

def prepare(state_path: Path = STATE):
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
    upstream, artifact, worker_id = "synthetic-original-job", "synthetic-original-output", "g70-ci-worker"
    with SessionLocal() as db:
        project = db.get(DBProject, pid)
        tenant_id = project.tenant_id
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
            status="RUNNING", progress=50, message="Synthetic worker awaiting artifact intake",
            request_json={"imageAssetId":image_id, "sourceAssetSha256":"a"*64},
            request_hash="b"*64,
            capability_snapshot_json={"quota":{"monthlyGeneratedSecondsLimit":100}},
            capability_snapshot_hash="c"*64, idempotency_key=str(uuid.uuid4()),
            upstream_job_id=upstream, provider_artifact_id=artifact,
            media_id=None, asset_version_id=None, attempts=1, max_attempts=1,
            lease_owner=worker_id, lease_expires_at=now + dt.timedelta(minutes=15),
            cancel_requested_at=None, error_code=None, error_message=None,
            created_at=now, updated_at=now, started_at=now, completed_at=None)
        attempt = DBGenerationAttempt(
            id=attempt_id, generation_task_id=task_id, attempt_no=1,
            status="RUNNING", submission_started_at=now,
            upstream_job_id=upstream, reconciliation_state="SUBMITTED",
            error_code=None, error_message=None, created_at=now, completed_at=None)
        reserved = DBGenerationUsageEntry(
            id=str(uuid.uuid4()), tenant_id=tenant_id, project_id=pid,
            task_id=task_id, attempt_id=attempt_id, kind="RESERVED",
            request_units=1, generated_seconds=0,
            reservation_key=f"generation:{task_id}", config_version_id=None, created_at=now)
        tenant = db.get(DBTenant, tenant_id)
        storage_before = tenant.used_storage_bytes
        # Flush parent rows before PostgreSQL enforces each dependent FK.
        db.add(source)
        db.flush()
        db.add(rights)
        db.add(task)
        db.flush()
        db.add(attempt)
        db.flush()
        db.add(reserved)
        db.commit()

    # Inject the failure at the final business-asset INSERT within the artifact-intake transaction.
    # The trigger is created only in the disposable SQLite CI database, scoped
    # to this random project and video assets; it is removed immediately after
    # the one HTTP request and is never part of application or production code.
    trigger_name = "g70_final_asset_insert_fault_" + uuid.uuid4().hex
    function_name = "g70_final_asset_insert_fault_fn_" + uuid.uuid4().hex
    with SessionLocal() as db:
        dialect = db.get_bind().dialect.name
        quote = db.get_bind().dialect.identifier_preparer.quote
        table_name = quote(DBAssetVersion.__tablename__)
        trigger_sql_name = quote(trigger_name)
        function_sql_name = quote(function_name)
        project_literal = pid.replace("'", "''")
        if dialect == "sqlite":
            db.execute(text(
                f"CREATE TRIGGER {trigger_sql_name} BEFORE INSERT ON {table_name} "
                f"WHEN NEW.project_id = '{project_literal}' AND NEW.media_type = 'video' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic G70 final asset INSERT fault'); END"
            ))
        elif dialect == "postgresql":
            db.execute(text(
                f"CREATE FUNCTION {function_sql_name}() RETURNS trigger "
                "LANGUAGE plpgsql AS $g70$ BEGIN "
                f"IF NEW.project_id = '{project_literal}' AND NEW.media_type = 'video' THEN "
                "RAISE EXCEPTION 'synthetic G70 final asset INSERT fault'; "
                "END IF; RETURN NEW; END; $g70$;"
            ))
            db.execute(text(
                f"CREATE TRIGGER {trigger_sql_name} BEFORE INSERT ON {table_name} "
                f"FOR EACH ROW EXECUTE FUNCTION {function_sql_name}()"
            ))
        else:
            raise AssertionError(f"unsupported G70 fault-injection dialect: {dialect}")
        db.commit()

    try:
        worker_token = os.getenv("AETHER_WORKER_TOKEN", "")
        assert worker_token.startswith("ci-") and len(worker_token) > 20
        with login() as client:
            with VIDEO.open("rb") as media_file:
                intake = client.post(
                    f"/internal/generation-tasks/{task_id}/artifact-intake",
                    data={"providerArtifactId": artifact},
                    files={"file": ("synthetic-original.mp4", media_file, "video/mp4")},
                    headers={"X-Worker-Token": worker_token, "X-Worker-Id": worker_id},
                )
            assert intake.status_code == 503, (
                f"expected injected final asset INSERT failure to fail closed; "
                f"status={intake.status_code} body={intake.text[:500]}"
            )
            assert intake.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
    finally:
        with SessionLocal() as db:
            quote = db.get_bind().dialect.identifier_preparer.quote
            table_name = quote(DBAssetVersion.__tablename__)
            if dialect == "sqlite":
                db.execute(text(f"DROP TRIGGER IF EXISTS {quote(trigger_name)}"))
            else:
                db.execute(text(f"DROP TRIGGER IF EXISTS {quote(trigger_name)} ON {table_name}"))
                db.execute(text(f"DROP FUNCTION IF EXISTS {quote(function_name)}()"))
            db.commit()

    with login() as client:
        endpoint = f"/projects/{pid}/generation-tasks/{task_id}/fake-recovery-check"
        found = client.get(endpoint)
        assert found.status_code == 200, found.text[:500]
        body = found.json()
        assert body["recoveryState"] == "FOUND_UNBOUND"
        assert body["sha256"] == sha and body["sizeBytes"] == len(original)
        assert body["mediaId"] and body["businessAssetBound"] is False
        assert body["financialSettlementVerified"] is False
        assert client.get(
            f"/projects/{other_id}/generation-tasks/{task_id}/fake-recovery-check"
        ).status_code == 404

    with SessionLocal() as db:
        intent = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_UPLOAD_INTENT")).scalar_one()
        held = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_HELD")).scalar_one()
        key = (intent.metadata_json or {}).get("fakeRecoveryKey")
        assert isinstance(key, str) and key.startswith("g70-")
        assert (held.metadata_json or {}).get("sidecarAcknowledged") is True
        assert (held.metadata_json or {}).get("storageReservationHeld") is True

    expected = {
        "status":"UNKNOWN", "error":"ARTIFACT_INGEST_FAILED",
        "businessAssetId":None, "mediaId":body["mediaId"],
        "attempts":1, "events":2, "videoAssets":0,
        "settlement":0, "reserved":1, "released":0, "relinked":0,
        "storageHeld":storage_before + len(original)}
    assert invariant(task_id, tenant_id) == expected
    state_path.write_text(json.dumps({
        "taskId":task_id,"tenantId":tenant_id,"projectId":pid,
        "otherProjectId":other_id,"mediaId":body["mediaId"],"sourceId":image_id,
        "key":key,"sha":sha,"size":len(original),"storageBefore":storage_before,
        "expected":expected}),encoding="utf8")
    print(json.dumps({
        "phase":"live_artifact_intake_final_asset_insert_fault",
        "fakeOnly":True,"fixture":"SYNTHETIC_DB_AND_FFMPEG",
        "faultInjection":"SQLite trigger rejected final video-asset INSERT after real Sidecar HTTP ACK",
        "apiStatus":intake.status_code,"apiCode":intake.json()["detail"]["code"],
        "recoveryState":body["recoveryState"],"sameTask":task_id,
        "attempts":1,"reserved":1,"settled":0,"released":0,
        "sha256":sha}),flush=True)

def verify(state_path: Path = STATE, mode: str = "serial"):
    gates()
    state = json.loads(state_path.read_text(encoding="utf8"))
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
    print(json.dumps({"phase":"after_restart_before_relink","taskId":state["taskId"],
        "recoveryState":"FOUND_UNBOUND","sha256":state["sha"],"fakeOnly":True}),flush=True)

    # Recover the SAME task that experienced the ambiguous upload ACK and DB failure.
    relink_url = (
        f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-relink"
    )
    bound = None
    if mode == "concurrent":
        barrier = Barrier(2)
        def relink_call():
            with login() as client:
                barrier.wait(timeout=15)
                response = client.post(relink_url)
                return response.status_code, response.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(relink_call) for _ in range(2)]
            answers = [future.result(timeout=90) for future in futures]
        assert all(status == 200 for status, _ in answers), answers
        bodies = [body for _, body in answers]
        fresh = [body for body in bodies if body.get("idempotent") is False]
        replayed = [body for body in bodies if body.get("idempotent") is True]
        assert len(fresh) == len(replayed) == 1, bodies
        bound, replay = fresh[0], replayed[0]
        assert bound["taskId"] == state["taskId"]
        assert bound["recoveryState"] == "BOUND_UNSETTLED"
        assert bound["actorMode"] == "OWNER_SELF"
        assert bound["businessAssetBound"] is True
        assert bound["financialSettlementVerified"] is False
        assert bound["settlementCreated"] is False
        assert bound["reservationPreserved"] is True
        assert bound["mediaId"] == state["mediaId"] and bound["sha256"] == state["sha"]
        assert replay["assetVersionId"] == bound["assetVersionId"]
    elif mode == "revoke-race":
        barrier = Barrier(2)
        rights_url = (
            f"/projects/{state['projectId']}/asset-versions/{state['sourceId']}/rights-snapshots"
        )
        revoke_payload = {
            "status": "REVOKED", "purpose": "REMIX", "territory": "TEST",
            "evidenceRef": "SYNTHETIC_G70_PG_CONCURRENT_REVOKE",
            "validFrom": None, "validUntil": None,
        }
        def relink_call():
            with login() as client:
                barrier.wait(timeout=15)
                response = client.post(relink_url)
                return response.status_code, response.json()
        def revoke_call():
            with login() as client:
                barrier.wait(timeout=15)
                response = client.post(rights_url, json=revoke_payload)
                return response.status_code, response.json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            relink_future = pool.submit(relink_call)
            revoke_future = pool.submit(revoke_call)
            relink_status, relink_body = relink_future.result(timeout=90)
            revoke_status, revoke_body = revoke_future.result(timeout=90)
        assert revoke_status == 201, (revoke_status, revoke_body)
        assert revoke_body["status"] == "REVOKED"
        if relink_status == 200:
            bound = relink_body
            assert bound["recoveryState"] == "BOUND_UNSETTLED"
            assert bound["idempotent"] is False
            with SessionLocal() as db:
                event = db.execute(select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == state["taskId"],
                    DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED",
                )).scalar_one()
                latest = db.execute(select(DBRightsSnapshot).where(
                    DBRightsSnapshot.asset_version_id == state["sourceId"],
                    DBRightsSnapshot.purpose == "REMIX",
                ).order_by(DBRightsSnapshot.captured_at.desc()).limit(1)).scalar_one()
                assert latest.status == "REVOKED"
                event_at = event.created_at
                if event_at.tzinfo is None:
                    event_at = event_at.replace(tzinfo=dt.timezone.utc)
                rights_at = dt.datetime.fromisoformat(
                    revoke_body["capturedAt"].replace("Z", "+00:00")
                )
                assert event_at <= rights_at, {
                    "relinkEventAt": event_at.isoformat(),
                    "revocationAt": rights_at.isoformat(),
                }
        elif relink_status == 409:
            assert relink_body.get("detail", {}).get("code") == "SOURCE_RIGHTS_REVOKED", relink_body
        else:
            raise AssertionError(f"unexpected concurrent relink response {relink_status}: {relink_body}")
        print(json.dumps({
            "phase": "concurrent_owner_relink_vs_rights_revoke",
            "fakeOnly": True,
            "relinkStatus": relink_status,
            "revocationStatus": revoke_status,
            "outcome": "RELINK_LINEARIZED_BEFORE_REVOKE" if bound else "REVOKE_LINEARIZED_BEFORE_RELINK",
        }), flush=True)
    elif mode == "serial":
        with login() as client:
            first = client.post(relink_url)
            assert first.status_code == 200, first.text[:500]
            bound = first.json()
            assert bound["taskId"] == state["taskId"]
            assert bound["recoveryState"] == "BOUND_UNSETTLED"
            assert bound["actorMode"] == "OWNER_SELF"
            assert bound["businessAssetBound"] is True
            assert bound["financialSettlementVerified"] is False
            assert bound["settlementCreated"] is False
            assert bound["reservationPreserved"] is True
            assert bound["idempotent"] is False
            assert bound["mediaId"] == state["mediaId"] and bound["sha256"] == state["sha"]
            second = client.post(relink_url)
            assert second.status_code == 200, second.text[:500]
            replay = second.json()
            assert replay["idempotent"] is True
            assert replay["assetVersionId"] == bound["assetVersionId"]
    else:
        raise AssertionError(f"unknown G70 verification mode: {mode}")

    with login() as client:
        foreign = client.post(
            f"/projects/{state['otherProjectId']}/generation-tasks/{state['taskId']}/fake-recovery-relink"
        )
        assert foreign.status_code == 404

    if bound is None:
        with SessionLocal() as db:
            task = db.get(DBGenerationTask, state["taskId"])
            assets = db.execute(select(DBAssetVersion).where(
                DBAssetVersion.project_id == state["projectId"],
                DBAssetVersion.media_id == state["mediaId"])).scalars().all()
            relinks = db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == state["taskId"],
                DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED")).scalar_one()
            attempts = db.execute(select(func.count(DBGenerationAttempt.id)).where(
                DBGenerationAttempt.generation_task_id == state["taskId"])).scalar_one()
            settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == state["taskId"],
                DBGenerationUsageEntry.kind == "SETTLED")).scalar_one()
            released = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == state["taskId"],
                DBGenerationUsageEntry.kind == "RELEASED")).scalar_one()
            reserved_count = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == state["taskId"],
                DBGenerationUsageEntry.kind == "RESERVED")).scalar_one()
            latest = db.execute(select(DBRightsSnapshot).where(
                DBRightsSnapshot.asset_version_id == state["sourceId"],
                DBRightsSnapshot.purpose == "REMIX"
            ).order_by(DBRightsSnapshot.captured_at.desc()).limit(1)).scalar_one()
            tenant = db.get(DBTenant, state["tenantId"])
            task_state = {"status": task.status, "error": task.error_code,
                          "assetVersionId": task.asset_version_id, "mediaId": task.media_id}
            assert task.status == "UNKNOWN" and task.error_code == "ARTIFACT_INGEST_FAILED", task_state
            assert task.asset_version_id is None and task.media_id == state["mediaId"], task_state
            assert latest.status == "REVOKED"
            assert len(assets) == relinks == settled == released == 0
            assert attempts == task.attempts == 1 and reserved_count == 1
            assert tenant.used_storage_bytes == state["storageBefore"] + state["size"]
        final_media = httpx.get(
            f"{SIDECAR}/media/{state['projectId']}/{state['mediaId']}",
            timeout=20, trust_env=False)
        assert final_media.status_code == 200
        assert len(final_media.content) == state["size"]
        assert hashlib.sha256(final_media.content).hexdigest() == state["sha"]
        print(json.dumps({
            "phase": "revoke_wins_fail_closed",
            "verified": True,
            "taskId": state["taskId"],
            "assetBound": False,
            "attempts": 1,
            "reserved": 1,
            "settled": 0,
            "released": 0,
            "providerResubmitted": False,
            "sha256": state["sha"],
        }), flush=True)
        return

    with SessionLocal() as db:
        task = db.get(DBGenerationTask, state["taskId"])
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == state["taskId"])).scalar_one()
        assets = db.execute(select(DBAssetVersion).where(
            DBAssetVersion.project_id == state["projectId"],
            DBAssetVersion.media_id == state["mediaId"])).scalars().all()
        relinks = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == state["taskId"],
            DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED")).scalar_one()
        attempts = db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == state["taskId"])).scalar_one()
        settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == state["taskId"],
            DBGenerationUsageEntry.kind == "SETTLED")).scalar_one()
        released = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == state["taskId"],
            DBGenerationUsageEntry.kind == "RELEASED")).scalar_one()
        reserved_count = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == state["taskId"],
            DBGenerationUsageEntry.kind == "RESERVED")).scalar_one()
        tenant = db.get(DBTenant, state["tenantId"])
        project = db.get(DBProject, state["projectId"])
        assert task.status == "UNKNOWN" and task.error_code == "ARTIFACT_RECOVERY_UNSETTLED"
        assert task.asset_version_id == bound["assetVersionId"] and task.media_id == state["mediaId"]
        assert attempt.reconciliation_state == "ASSET_RELINKED_UNSETTLED"
        assert len(assets) == 1 and assets[0].sha256 == state["sha"]
        assert assets[0].probe_json["provenance"]["generationTaskId"] == state["taskId"]
        assert relinks == 1 and attempts == 1 and task.attempts == 1
        assert settled == 0 and released == 0 and reserved_count == 1
        assert tenant.used_storage_bytes == state["storageBefore"] + state["size"]
        assert len([item for item in (project.materials or []) if item.get("id") == state["mediaId"]]) == 1

    final_media = httpx.get(
        f"{SIDECAR}/media/{state['projectId']}/{state['mediaId']}",
        timeout=20, trust_env=False)
    assert final_media.status_code == 200
    assert len(final_media.content) == state["size"]
    assert hashlib.sha256(final_media.content).hexdigest() == state["sha"]
    print(json.dumps({"phase":"asset_relinked","verified":True,"verificationMode":mode,
        "taskId":state["taskId"],"sameTaskAsUploadFailure":True,
        "fixture":"REAL_COMPOSE_SIDECAR_AND_FFMPEG",
        "actorMode":"OWNER_SELF","assetRelink":True,"idempotentRetry":True,
        "reserved":1,"settled":0,"released":0,"providerResubmitted":False,
        "sha256":state["sha"],"assetVersionId":bound["assetVersionId"]}),flush=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify", "verify-concurrent",
                                         "prepare-revoke-race", "verify-revoke-race"))
    phase = parser.parse_args().phase
    if phase == "prepare":
        prepare(STATE)
    elif phase == "verify":
        verify(STATE, mode="serial")
    elif phase == "verify-concurrent":
        verify(STATE, mode="concurrent")
    elif phase == "prepare-revoke-race":
        prepare(STATE_REVOKE_RACE)
    else:
        verify(STATE_REVOKE_RACE, mode="revoke-race")
