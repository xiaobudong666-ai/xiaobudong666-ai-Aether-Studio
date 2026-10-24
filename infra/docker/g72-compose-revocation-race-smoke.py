"""G72 FAKE-only real Sidecar HTTP revocation race (disposable Compose only).

This test runs a native FastAPI TestClient against the disposable Compose SQLite
database while the *real* video-use service runs in a separate container. It
inserts a synthetic REMIX revocation after the HTTP upload has succeeded and
before the API adopts/settles the asset. The Worker is deliberately stopped:
this is an intake race test, NOT a second proof of API->Worker dispatch or
M12 trusted rights issuance. Never run with production credentials or media.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import engine, get_db, SessionLocal
from app.main import create_app
from app.models import (
    DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
    DBGenerationTask, DBGenerationUsageEntry, DBProject, DBRightsSnapshot,
    DBTenant, DBUser,
)
from app.video_use_adapter import VideoUseAdapter


def checked_disposable_environment() -> tuple[str, str]:
    assert os.environ.get("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.environ.get("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.environ.get("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert engine.dialect.name == "sqlite", "This test is only for disposable Compose SQLite"
    token = os.environ.get("AETHER_WORKER_TOKEN", "")
    password = os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "")
    assert token.startswith("ci-") and len(token) >= 24
    assert password.startswith("ci-") and len(password) >= 24
    # The companion workflow must stop Worker to make manual claim deterministic.
    return token, password


def make_real_mp4() -> bytes:
    with tempfile.TemporaryDirectory(prefix="g72-race-") as folder:
        mp4 = Path(folder) / "race.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=64x64:r=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(mp4),
            ],
            check=True, timeout=45,
        )
        payload = mp4.read_bytes()
        assert len(payload) > 1000 and payload[4:8] == b"ftyp"
        return payload


def image_fixture(project_id: str) -> tuple[str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        media_id = "synthetic-g72-race-image-" + uuid.uuid4().hex
        asset = DBAssetVersion(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id,
            project_id=project.id, media_id=media_id, version_no=1,
            sha256="a" * 64, media_type="image", content_type="image/png",
            size_bytes=100, probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id, created_at=now,
        )
        rights = DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id,
            project_id=project.id, asset_version_id=asset.id,
            status="ALLOWED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="synthetic-nonproduction-race-allowed",
            captured_by=project.owner_id, captured_at=now,
        )
        db.add_all([asset, rights])
        db.commit()
        return asset.id, project.tenant_id


class RevocationAfterRealHTTPUpload:
    def __init__(self, original: VideoUseAdapter, image_id: str):
        self.original = original
        self.image_id = image_id
        self.calls = 0
        self.receipt = None

    def __getattr__(self, name):
        return getattr(self.original, name)

    def upload_media(self, *args, **kwargs):
        assert self.calls == 0, "Never upload this test output twice"
        receipt = self.original.upload_media(*args, **kwargs)
        self.calls += 1
        self.receipt = receipt
        assert receipt.get("mediaId") and receipt.get("metadata", {}).get("video"), receipt
        # Inject a committed synthetic revocation while the API request is
        # still between the Sidecar HTTP ACK and the business-asset SQL write.
        with SessionLocal() as db:
            old = db.execute(select(DBRightsSnapshot).where(
                DBRightsSnapshot.asset_version_id == self.image_id,
            ).order_by(DBRightsSnapshot.captured_at.desc())).scalars().first()
            assert old is not None
            db.add(DBRightsSnapshot(
                id=str(uuid.uuid4()), tenant_id=old.tenant_id,
                project_id=old.project_id, asset_version_id=self.image_id,
                status="REVOKED", purpose="REMIX", territory="TEST",
                valid_from=None, valid_until=None,
                evidence_ref="synthetic-inflight-revocation-after-real-http",
                captured_by=old.captured_by,
                captured_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
            ))
            db.commit()
        return receipt


def run() -> None:
    token, password = checked_disposable_environment()
    source = make_real_mp4()
    expected_sha = hashlib.sha256(source).hexdigest()
    owner_email = os.environ.get("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local")
    adapter = VideoUseAdapter(api_url="http://video-use:8002", timeout=30, max_retries=0)
    app = create_app(
        app_engine=engine, db_dependency=get_db, video_use_adapter=adapter,
        bootstrap_admin_password=password, bootstrap_admin_email=owner_email,
        worker_token=token, cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    with TestClient(app) as client:
        login = client.post("/auth/login", json={
            "email": owner_email, "password": password,
        })
        assert login.status_code == 200, f"Owner login={login.status_code}"
        client.headers.update({"X-Aether-CSRF": "1"})
        created = client.post("/projects", json={
            "name": "Synthetic G72 revoked mid-upload " + uuid.uuid4().hex[:10],
        })
        assert created.status_code == 201, created.text
        project = created.json()
        pid = project["id"]
        image_id, tenant_id = image_fixture(pid)
        wrapped = RevocationAfterRealHTTPUpload(adapter, image_id)
        app.state.video_use = wrapped
        caps = client.get("/generation/providers/moneyprinter/capabilities")
        assert caps.status_code == 200, caps.text
        payload = {
            "text": "Synthetic revocation during real Sidecar HTTP upload",
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
        create = client.post(base, json=payload)
        assert create.status_code == 202, create.text
        task_id = create.json()["taskId"]
        worker_id = "ci-g72-revocation-worker"
        headers = {"X-Worker-Token": token, "X-Worker-Id": worker_id}
        claimed = client.post("/internal/generation-tasks/claim", headers=headers)
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["taskId"] == task_id, "Worker must be stopped during synthetic claim"
        for status in ("RUNNING", "INGESTING"):
            response = client.post(
                f"/internal/generation-tasks/{task_id}/transition",
                headers=headers,
                json={
                    "status": status, "progress": 60 if status == "RUNNING" else 95,
                    "message": "synthetic-" + status,
                    "upstreamJobId": "ci-fake-provider-job-revocation",
                    **({"providerArtifactId": "ci-fake-artifact-revocation"} if status == "INGESTING" else {}),
                },
            )
            assert response.status_code == 200, response.text
        with SessionLocal() as db:
            before_storage = db.get(DBTenant, tenant_id).used_storage_bytes
        ingested = client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers=headers, data={"providerArtifactId": "ci-fake-artifact-revocation"},
            files={"file": ("real-sidecar-race.mp4", source, "video/mp4")},
        )
        assert ingested.status_code == 503, ingested.text
        assert ingested.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
        assert wrapped.calls == 1
        media_id = wrapped.receipt["mediaId"]
        assert wrapped.receipt["sha256"] == expected_sha
        with SessionLocal() as db:
            task = db.get(DBGenerationTask, task_id)
            assert task.status == "UNKNOWN" and task.asset_version_id is None
            assert task.media_id == media_id and task.attempts == 1
            assert db.execute(select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == pid, DBAssetVersion.media_type == "video",
            )).scalar_one() == 0
            assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )).scalar_one() == 0
            assert db.get(DBTenant, tenant_id).used_storage_bytes == before_storage + len(source)
            for event_type in ("ARTIFACT_UPLOAD_INTENT", "ARTIFACT_RECOVERY_HELD"):
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == event_type,
                )).scalar_one() == 1
            attempt_count = db.execute(select(func.count(DBGenerationAttempt.id)).where(
                DBGenerationAttempt.generation_task_id == task_id,
            )).scalar_one()
            assert attempt_count == 1
        # Raw media is physically persisted in a different running container.
        physical = httpx.get(
            f"http://video-use:8002/media/{pid}/{media_id}",
            timeout=30, trust_env=False,
        )
        assert physical.status_code == 200
        assert hashlib.sha256(physical.content).hexdigest() == expected_sha
        assert client.get(
            f"/projects/{pid}/generation-tasks/{task_id}/fake-recovery-check"
        ).status_code == 409, "revoked source must block Owner's recovery"
        assert client.post(
            f"/projects/{pid}/generation-tasks/{task_id}/retry"
        ).status_code == 409
        again = client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers=headers, data={"providerArtifactId": "ci-fake-artifact-revocation"},
            files={"file": ("duplicate.mp4", source, "video/mp4")},
        )
        assert again.status_code == 409 and wrapped.calls == 1
        print(json.dumps({
            "phase": "g72_revocation_after_real_sidecar_http",
            "testLevel": "NATIVE_API_TESTCLIENT_COMPOSE_REAL_SIDECAR_HTTP_SQLITE",
            "fakeOnly": True, "sourceRights": "SYNTHETIC_REVOKED",
            "taskStatus": "UNKNOWN", "assetBound": False,
            "settledEntries": 0, "originalMediaSha256": expected_sha,
            "uploadCalls": wrapped.calls, "attempts": attempt_count,
            "reservationHeld": True, "retryBlocked": True,
            "ownerRecoveryBlocked": True,
        }), flush=True)


if __name__ == "__main__":
    run()
