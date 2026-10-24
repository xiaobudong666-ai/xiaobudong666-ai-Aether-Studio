"""G72 FAKE-ONLY: real API source + real HTTP sidecar, revocation during upload.

This is a disposable cross-container component test, NOT an end-to-end
Worker concurrency, PostgreSQL, M12 trusted issuance, or release acceptance.
The permission change is a synthetic DB fixture inserted after the sidecar
returns its physical-media receipt and before API business-asset binding.
"""
from __future__ import annotations

import datetime as dt
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

STATE = Path("/tmp/g72-midupload-revocation-state.json")
INPUT = Path("/tmp/g72-midupload-ci.mp4")
VIDEO_USE = "http://video-use:8002"


def guard():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert os.getenv("DATABASE_URL") == "sqlite:////data/aether.db"
    assert os.getenv("VIDEO_USE_API_URL") == VIDEO_USE
    assert os.getenv("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "").startswith("ci-")
    assert os.getenv("AETHER_WORKER_TOKEN", "").startswith("ci-")


class RevokingSidecar(VideoUseAdapter):
    def __init__(self):
        super().__init__(api_url=VIDEO_USE, max_retries=0)
        self.source_id: str | None = None
        self.receipt: dict | None = None
        self.recovery_key: str | None = None

    def upload_media(self, project_id, filename, content_type, stream, **kwargs):
        guard()
        assert self.source_id and self.receipt is None
        assert kwargs.get("recovery_key") and kwargs.get("expected_sha256")
        # Real API container -> independent video-use container over HTTP.
        result = super().upload_media(project_id, filename, content_type, stream, **kwargs)
        assert result.get("projectId") == project_id and result.get("mediaId")
        self.receipt = result
        self.recovery_key = kwargs["recovery_key"]
        # Deterministic fault insertion at the post-ACK / pre-business-commit
        # boundary. No application code or live Provider is monkeypatched.
        with SessionLocal() as db:
            source = db.get(DBAssetVersion, self.source_id)
            assert source is not None and source.project_id == project_id
            old = db.execute(select(DBRightsSnapshot).where(
                DBRightsSnapshot.asset_version_id == self.source_id,
                DBRightsSnapshot.purpose == "REMIX",
            ).order_by(DBRightsSnapshot.captured_at.desc())).scalars().first()
            assert old is not None and old.status == "ALLOWED"
            db.add(DBRightsSnapshot(
                id=str(uuid.uuid4()), tenant_id=source.tenant_id,
                project_id=project_id, asset_version_id=self.source_id,
                status="REVOKED", purpose="REMIX", territory="TEST",
                valid_from=None, valid_until=None,
                evidence_ref="SYNTHETIC_CI_G72_POST_ACK_REVOCATION",
                captured_by=old.captured_by,
                captured_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
            ))
            db.commit()
        return result


def owner_client(sidecar: VideoUseAdapter):
    app = create_app(
        app_engine=engine, db_dependency=get_db, video_use_adapter=sidecar,
        cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    return TestClient(app)


def login(client):
    response = client.post("/auth/login", json={
        "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
        "password": os.environ["AETHER_BOOTSTRAP_ADMIN_PASSWORD"],
    })
    assert response.status_code == 200, f"Owner login: {response.status_code}"
    client.headers["X-Aether-CSRF"] = "1"


def seed_image(project_id: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        source_id = str(uuid.uuid4())
        db.add(DBAssetVersion(
            id=source_id, tenant_id=project.tenant_id, project_id=project_id,
            media_id="synthetic-g72-midupload-" + uuid.uuid4().hex,
            version_no=1, sha256="b" * 64, media_type="image",
            content_type="image/png", size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id,
            project_id=project_id, asset_version_id=source_id,
            status="ALLOWED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None, evidence_ref="SYNTHETIC_CI_ALLOWED",
            captured_by=project.owner_id, captured_at=now,
        ))
        db.commit()
        return source_id


def counts(task_id: str, tenant_id: str) -> dict:
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.tenant_id == tenant_id
        tenant = db.get(DBTenant, tenant_id)
        assert tenant is not None
        def count(table, *filters):
            return db.execute(select(func.count(table.id)).where(*filters)).scalar_one()
        return {
            "taskStatus": task.status,
            "mediaId": task.media_id,
            "boundAssetId": task.asset_version_id,
            "attempts": count(DBGenerationAttempt, DBGenerationAttempt.generation_task_id == task_id),
            "intent": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                            DBGenerationEvent.event_type == "ARTIFACT_UPLOAD_INTENT"),
            "held": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                          DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_HELD"),
            "ingested": count(DBGenerationEvent, DBGenerationEvent.generation_task_id == task_id,
                              DBGenerationEvent.event_type == "ARTIFACT_INGESTED"),
            "settled": count(DBGenerationUsageEntry, DBGenerationUsageEntry.task_id == task_id,
                             DBGenerationUsageEntry.kind == "SETTLED"),
            "videoAssetCount": count(DBAssetVersion, DBAssetVersion.project_id == task.project_id,
                                     DBAssetVersion.media_type == "video"),
            "usedStorageBytes": tenant.used_storage_bytes,
        }


def assert_held(row: dict, state: dict):
    assert row["taskStatus"] == "UNKNOWN", row
    assert row["mediaId"] == state["mediaId"], row
    assert row["boundAssetId"] is None
    assert row["attempts"] == row["intent"] == row["held"] == 1, row
    assert row["ingested"] == row["settled"] == row["videoAssetCount"] == 0, row
    assert row["usedStorageBytes"] == state["storageBefore"] + state["bytes"], row


def physical_media(state: dict):
    response = httpx.get(
        f"{VIDEO_USE}/media/{state['projectId']}/{state['mediaId']}",
        timeout=30, trust_env=False,
    )
    assert response.status_code == 200, f"sidecar media: {response.status_code}"
    assert len(response.content) == state["bytes"]
    assert response.content[4:8] == b"ftyp"
    assert hashlib.sha256(response.content).hexdigest() == state["sha256"]
    # Idempotent recovery is a READ and must not trigger another sidecar POST.
    found = VideoUseAdapter(api_url=VIDEO_USE).find_fake_recovery(
        state["projectId"], state["recoveryKey"], state["sha256"],
    )
    assert found["mediaId"] == state["mediaId"]
    assert found["sha256"] == state["sha256"]


def prepare():
    guard()
    data = INPUT.read_bytes()
    assert len(data) > 1000 and data[4:8] == b"ftyp"
    sha = hashlib.sha256(data).hexdigest()
    sidecar = RevokingSidecar()
    with owner_client(sidecar) as client:
        login(client)
        created = client.post("/projects", json={
            "name": "G72 synthetic mid-upload revoke " + uuid.uuid4().hex[:12],
        })
        assert created.status_code == 201, created.text
        project = created.json()
        pid = project["id"]
        image_id = seed_image(pid)
        sidecar.source_id = image_id
        caps = client.get("/generation/providers/moneyprinter/capabilities")
        assert caps.status_code == 200, caps.text
        payload = {
            "text": "G72 synthetic revocation during physical upload",
            "durationSeconds": 1, "aspectRatio": "9:16",
            "resolution": "720p", "voiceId": "synthetic-seed",
            "imageAssetVersionId": image_id,
            "idempotencyKey": str(uuid.uuid4()),
            "expectedProjectRevision": project["revision"],
            "capabilitySnapshotHash": caps.json()["snapshotHash"],
            "confirmSyntheticTestOnly": True,
        }
        base = f"/projects/{pid}/talking-head/fake-generation-tasks"
        preflight = client.post(base + "/validate", json=payload)
        assert preflight.status_code == 200, preflight.text
        created_task = client.post(base, json=payload)
        assert created_task.status_code == 202, created_task.text
        task_id = created_task.json()["taskId"]
        headers = {
            "X-Worker-Token": os.environ["AETHER_WORKER_TOKEN"],
            "X-Worker-Id": "g72-revocation-test-worker",
        }
        claimed = client.post("/internal/generation-tasks/claim", headers=headers)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["taskId"] == task_id
        for status in ("RUNNING", "INGESTING"):
            transitioned = client.post(
                f"/internal/generation-tasks/{task_id}/transition",
                headers=headers,
                json={
                    "status": status,
                    "progress": 5 if status == "RUNNING" else 90,
                    "message": "Synthetic FAKE-only source rights timing test",
                    "upstreamJobId": "ci-g72-offline-original",
                    "providerArtifactId": "ci-g72-offline-artifact" if status == "INGESTING" else None,
                },
            )
            assert transitioned.status_code == 200, transitioned.text
        with SessionLocal() as db:
            tenant_id = db.get(DBProject, pid).tenant_id
            storage_before = db.get(DBTenant, tenant_id).used_storage_bytes
        intake = client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers=headers, data={"providerArtifactId": "ci-g72-offline-artifact"},
            files={"file": ("ci-g72-midupload.mp4", data, "video/mp4")},
        )
        assert intake.status_code == 503, intake.text
        assert intake.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
        assert sidecar.receipt and sidecar.recovery_key
        state = {
            "projectId": pid, "tenantId": tenant_id,
            "taskId": task_id, "mediaId": sidecar.receipt["mediaId"],
            "recoveryKey": sidecar.recovery_key, "sha256": sha,
            "bytes": len(data), "storageBefore": storage_before,
        }
        row = counts(task_id, tenant_id)
        assert_held(row, state)
        denied = client.get(f"/projects/{pid}/generation-tasks/{task_id}/fake-recovery-check")
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"
        duplicate = client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers=headers, data={"providerArtifactId": "ci-g72-offline-artifact"},
            files={"file": ("ci-g72-midupload.mp4", data, "video/mp4")},
        )
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"
        assert counts(task_id, tenant_id) == row
        physical_media(state)
        STATE.write_text(json.dumps(state), encoding="utf8")
        print(json.dumps({
            "phase": "post_ack_revocation_held", "fakeOnly": True,
            "sourceRights": "SYNTHETIC_REVOKED",
            "crossContainerHttp": True, "result": row, "sha256": sha,
        }), flush=True)


def verify():
    guard()
    state = json.loads(STATE.read_text(encoding="utf8"))
    physical_media(state)
    row = counts(state["taskId"], state["tenantId"])
    assert_held(row, state)
    with owner_client(VideoUseAdapter(api_url=VIDEO_USE)) as client:
        login(client)
        task = client.get(
            f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}"
        )
        assert task.status_code == 200 and task.json()["storedStatus"] == "UNKNOWN"
        denied = client.get(
            f"/projects/{state['projectId']}/generation-tasks/{state['taskId']}/fake-recovery-check"
        )
        assert denied.status_code == 409
        assert denied.json()["detail"]["code"] == "SOURCE_RIGHTS_REVOKED"
    assert counts(state["taskId"], state["tenantId"]) == row
    print(json.dumps({
        "phase": "after_worker_sidecar_restart", "fakeOnly": True,
        "physicalMediaSameSha": True, "noBusinessAsset": True,
        "noSettlement": True, "noRetry": True, "sourceRightsTrusted": False,
        "result": row,
    }), flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify"))
    args = parser.parse_args()
    (prepare if args.phase == "prepare" else verify)()
