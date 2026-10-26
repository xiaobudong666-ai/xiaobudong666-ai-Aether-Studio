"""G71 FAKE-only: Sidecar pre-ACK outage and exact-original-byte manual recovery.

Runs against the disposable PostgreSQL/API/Sidecar Compose stack only. It never
recreates a generation attempt, calls a Provider, settles/refunds usage, or
releases a held reservation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, "/app")
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal, engine, get_db
from app.main import create_app
from app.models import (
    DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
    DBGenerationTask, DBGenerationUsageEntry, DBProject, DBRightsSnapshot, DBTenant,
)
from app.video_use_adapter import VideoUseAdapter

STATE = Path("/tmp/g71-preack-manual-retry-state.json")
INPUT = Path("/tmp/g71-preack-original.mp4")
VIDEO_USE = "http://video-use:8002"


def guard():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert os.getenv("DATABASE_URL") == (
        "postgresql+psycopg://ci_g72:ci-only-postgres-password@postgres-g72:5432/aether_g72_ci"
    )
    assert engine.dialect.name == "postgresql"
    assert os.getenv("VIDEO_USE_API_URL") == VIDEO_USE
    assert os.getenv("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "").startswith("ci-")
    assert os.getenv("AETHER_WORKER_TOKEN", "").startswith("ci-")


def owner_client():
    app = create_app(
        app_engine=engine, db_dependency=get_db,
        video_use_adapter=VideoUseAdapter(api_url=VIDEO_USE, max_retries=0),
        cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    return TestClient(app)


def login(client):
    response = client.post("/auth/login", json={
        "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
        "password": os.environ["AETHER_BOOTSTRAP_ADMIN_PASSWORD"],
    })
    assert response.status_code == 200, f"Owner login: {response.status_code} {response.text}"
    client.headers["X-Aether-CSRF"] = "1"


def seed_image(project_id: str) -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        source_id = str(uuid.uuid4())
        db.add(DBAssetVersion(
            id=source_id, tenant_id=project.tenant_id, project_id=project_id,
            media_id="synthetic-g71-source-" + uuid.uuid4().hex,
            version_no=1, sha256="c" * 64, media_type="image",
            content_type="image/png", size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id, project_id=project_id,
            asset_version_id=source_id, status="ALLOWED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_CI_G71_ALLOWED",
            captured_by=project.owner_id, captured_at=now,
        ))
        db.commit()
        return source_id


def counts(task_id: str, tenant_id: str, project_id: str) -> dict:
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.tenant_id == tenant_id and task.project_id == project_id
        def count(table, *filters):
            return db.execute(select(func.count(table.id)).where(*filters)).scalar_one()
        return {
            "status": task.status,
            "errorCode": task.error_code,
            "mediaId": task.media_id,
            "assetVersionId": task.asset_version_id,
            "attempts": count(DBGenerationAttempt, DBGenerationAttempt.generation_task_id == task_id),
            "reserved": count(DBGenerationUsageEntry, DBGenerationUsageEntry.task_id == task_id,
                              DBGenerationUsageEntry.kind == "RESERVED"),
            "released": count(DBGenerationUsageEntry, DBGenerationUsageEntry.task_id == task_id,
                              DBGenerationUsageEntry.kind == "RELEASED"),
            "settled": count(DBGenerationUsageEntry, DBGenerationUsageEntry.task_id == task_id,
                             DBGenerationUsageEntry.kind == "SETTLED"),
            "intent": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                            DBGenerationEvent.event_type == "ARTIFACT_UPLOAD_INTENT"),
            "held": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                          DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_HELD"),
            "reuploadAudit": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                                   DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_REUPLOAD"),
            "relinked": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                              DBGenerationEvent.event_type == "ARTIFACT_ASSET_RELINKED"),
            "ingested": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                              DBGenerationEvent.event_type == "ARTIFACT_INGESTED"),
            "videoAssets": count(DBAssetVersion, DBAssetVersion.project_id == project_id,
                                 DBAssetVersion.media_type == "video"),
            "usedStorageBytes": db.get(DBTenant, tenant_id).used_storage_bytes,
        }


def prepare():
    guard()
    data = INPUT.read_bytes()
    assert len(data) > 1000 and data[4:8] == b"ftyp"
    sha = hashlib.sha256(data).hexdigest()
    with owner_client() as client:
        login(client)
        created = client.post("/projects", json={
            "name": "G71 synthetic pre-ACK recovery " + uuid.uuid4().hex[:12],
        })
        assert created.status_code == 201, created.text
        project = created.json()
        project_id = project["id"]
        source_id = seed_image(project_id)
        caps = client.get("/generation/providers/moneyprinter/capabilities")
        assert caps.status_code == 200, caps.text
        payload = {
            "text": "G71 synthetic original artifact pre-ACK outage",
            "durationSeconds": 1, "aspectRatio": "9:16",
            "resolution": "720p", "voiceId": "synthetic-seed",
            "imageAssetVersionId": source_id,
            "idempotencyKey": str(uuid.uuid4()),
            "expectedProjectRevision": project["revision"],
            "capabilitySnapshotHash": caps.json()["snapshotHash"],
            "confirmSyntheticTestOnly": True,
        }
        base = f"/projects/{project_id}/talking-head/fake-generation-tasks"
        for _ in range(3):
            preflight = client.post(base + "/validate", json=payload)
            if preflight.status_code == 409 and preflight.json().get("detail", {}).get("code") == "CAPABILITY_SNAPSHOT_UNKNOWN":
                updated = client.get("/generation/providers/moneyprinter/capabilities")
                assert updated.status_code == 200, updated.text
                payload["capabilitySnapshotHash"] = updated.json()["snapshotHash"]
                continue
            assert preflight.status_code == 200, preflight.text
            created_task = client.post(base, json=payload)
            if created_task.status_code == 409 and created_task.json().get("detail", {}).get("code") == "CAPABILITY_SNAPSHOT_UNKNOWN":
                updated = client.get("/generation/providers/moneyprinter/capabilities")
                assert updated.status_code == 200, updated.text
                payload["capabilitySnapshotHash"] = updated.json()["snapshotHash"]
                continue
            assert created_task.status_code == 202, created_task.text
            break
        else:
            raise AssertionError("Capability snapshot kept rotating")
        task_id = created_task.json()["taskId"]
        worker_headers = {
            "X-Worker-Token": os.environ["AETHER_WORKER_TOKEN"],
            "X-Worker-Id": "g71-preack-test-worker",
        }
        claimed = client.post("/internal/generation-tasks/claim", headers=worker_headers)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["taskId"] == task_id, claimed.json()
        for status, progress, artifact_id in (
            ("RUNNING", 5, None),
            ("INGESTING", 90, "ci-g71-original-artifact"),
        ):
            response = client.post(
                f"/internal/generation-tasks/{task_id}/transition",
                headers=worker_headers,
                json={
                    "status": status, "progress": progress,
                    "message": "G71 synthetic pre-ACK test",
                    "upstreamJobId": "ci-g71-original-upstream-job",
                    "providerArtifactId": artifact_id,
                },
            )
            assert response.status_code == 200, response.text
        with SessionLocal() as db:
            tenant_id = db.get(DBProject, project_id).tenant_id
            storage_before = db.get(DBTenant, tenant_id).used_storage_bytes
        STATE.write_text(json.dumps({
            "phase": "prepared", "projectId": project_id, "tenantId": tenant_id,
            "taskId": task_id, "sourceId": source_id, "sha256": sha,
            "sizeBytes": len(data), "storageBefore": storage_before,
        }), encoding="utf8")
        print(json.dumps({"phase": "prepared", "fakeOnly": True, "taskId": task_id,
                          "sha256": sha, "sizeBytes": len(data)}), flush=True)


def inject_preack_failure():
    guard()
    state = json.loads(STATE.read_text(encoding="utf8"))
    assert state["phase"] == "prepared"
    data = INPUT.read_bytes()
    assert len(data) == state["sizeBytes"]
    assert hashlib.sha256(data).hexdigest() == state["sha256"]
    with owner_client() as client:
        login(client)
        before = counts(state["taskId"], state["tenantId"], state["projectId"])
        assert before["status"] == "INGESTING" and before["assetVersionId"] is None
        response = client.post(
            f"/internal/generation-tasks/{state['taskId']}/artifact-intake",
            headers={
                "X-Worker-Token": os.environ["AETHER_WORKER_TOKEN"],
                "X-Worker-Id": "g71-preack-test-worker",
            },
            data={"providerArtifactId": "ci-g71-original-artifact"},
            files={"file": ("generation-original.mp4", data, "video/mp4")},
        )
        assert response.status_code == 503, response.text
        assert response.json().get("detail", {}).get("code") == "ARTIFACT_RECOVERY_REQUIRED", response.text
        row = counts(state["taskId"], state["tenantId"], state["projectId"])
        assert row["status"] == "UNKNOWN" and row["errorCode"] == "ARTIFACT_INGEST_FAILED", row
        assert row["attempts"] == row["reserved"] == row["intent"] == row["held"] == 1, row
        assert row["released"] == row["settled"] == row["ingested"] == row["videoAssets"] == 0, row
        assert row["reuploadAudit"] == row["relinked"] == 0, row
        assert row["assetVersionId"] is None and row["mediaId"] is None, row
        assert row["usedStorageBytes"] == state["storageBefore"] + state["sizeBytes"], row
        retry = client.post(f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/retry")
        assert retry.status_code == 409
        assert retry.json().get("detail", {}).get("code") == "ARTIFACT_RECOVERY_REQUIRED"
        assert counts(state["taskId"], state["tenantId"], state["projectId"]) == row
        state["phase"] = "preack_failed"
        STATE.write_text(json.dumps(state), encoding="utf8")
        print(json.dumps({"phase": "pre_ack_sidecar_unavailable", "fakeOnly": True,
                          "apiStatus": response.status_code, "apiCode": response.json()["detail"]["code"],
                          "result": row, "sameAttemptOnly": True, "noNewGeneration": True}), flush=True)


def manual_retry():
    guard()
    state = json.loads(STATE.read_text(encoding="utf8"))
    assert state["phase"] == "preack_failed"
    data = INPUT.read_bytes()
    assert len(data) == state["sizeBytes"]
    assert hashlib.sha256(data).hexdigest() == state["sha256"]
    with owner_client() as client:
        login(client)
        check_url = f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-check"
        absent = client.get(check_url)
        assert absent.status_code == 503
        assert absent.json().get("detail", {}).get("code") == "ORIGINAL_MEDIA_LOOKUP_UNAVAILABLE"
        retry_url = f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-upload-retry"
        first = client.post(retry_url, files={"file": ("same-original.mp4", data, "video/mp4")})
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["recoveryState"] == "FOUND_UNBOUND"
        assert first_body["uploadPerformed"] is True and first_body["idempotent"] is False
        assert first_body["sha256"] == state["sha256"] and first_body["sizeBytes"] == state["sizeBytes"]
        second = client.post(retry_url, files={"file": ("same-original.mp4", data, "video/mp4")})
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["mediaId"] == first_body["mediaId"]
        assert second_body["uploadPerformed"] is False and second_body["idempotent"] is True
        assert second_body["sha256"] == state["sha256"]
        checked = client.get(check_url)
        assert checked.status_code == 200, checked.text
        assert checked.json()["mediaId"] == first_body["mediaId"]
        relink_url = f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-relink"
        relink = client.post(relink_url)
        assert relink.status_code == 200, relink.text
        assert relink.json()["recoveryState"] == "BOUND_UNSETTLED"
        replay = client.post(relink_url)
        assert replay.status_code == 200, replay.text
        assert replay.json()["idempotent"] is True
        response = httpx.get(
            f"{VIDEO_USE}/media/{state['projectId']}/{first_body['mediaId']}",
            timeout=30, trust_env=False,
        )
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == state["sha256"]
        row = counts(state["taskId"], state["tenantId"], state["projectId"])
        assert row["status"] == "UNKNOWN" and row["errorCode"] == "ARTIFACT_RECOVERY_UNSETTLED", row
        assert row["assetVersionId"] == relink.json()["assetVersionId"]
        assert row["attempts"] == row["reserved"] == row["intent"] == row["held"] == 1, row
        assert row["reuploadAudit"] == 2 and row["relinked"] == 1, row
        assert row["released"] == row["settled"] == row["ingested"] == 0, row
        assert row["videoAssets"] == 1, row
        assert row["usedStorageBytes"] == state["storageBefore"] + state["sizeBytes"], row
        print(json.dumps({
            "phase": "g71_same_original_manual_retry_recovered",
            "fakeOnly": True, "sameTask": state["taskId"], "attempts": row["attempts"],
            "sha256": state["sha256"], "mediaId": first_body["mediaId"],
            "firstUploadPerformed": first_body["uploadPerformed"],
            "secondRetryIdempotent": second_body["idempotent"],
            "relinkIdempotent": replay.json()["idempotent"],
            "noNewGeneration": True, "settled": row["settled"], "released": row["released"],
            "result": row,
        }), flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "inject-preack-failure", "manual-retry"))
    args = parser.parse_args()
    {"prepare": prepare, "inject-preack-failure": inject_preack_failure,
     "manual-retry": manual_retry}[args.phase]()
