"""Disposable G72 FAKE-ONLY cross-container smoke test.

Run inside an isolated API container using the FAKE-ONLY Compose override.
The input image/rights are *synthetic database fixtures*, not real M12 proofs.
No real Provider or production credentials or services may be used.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

# CI copies this script to /tmp; the API image installs native modules in /app.
# Resolve imports explicitly, without changing application or Provider settings.
sys.path.insert(0, "/app")

import httpx
from sqlalchemy import func, select
from app.database import SessionLocal
from app.auth import hash_password
from app.models import (
    DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
    DBGenerationTask, DBGenerationUsageEntry, DBProject, DBRightsSnapshot,
    DBTenant, DBUser,
)

STATE_PATH = Path("/tmp/g72-ci-fake-only-network-state.json")
API_BASE = "http://127.0.0.1:8000"
VIDEO_USE_BASE = "http://video-use:8002"


@contextmanager
def authenticate(*, email: str | None = None, password_override: str | None = None):
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    password = password_override or os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD")
    assert password and password.startswith("ci-"), "disposable CI credential required"
    # Open/close once; a client used for login cannot be entered a second time.
    with httpx.Client(base_url=API_BASE, timeout=30, trust_env=False) as client:
        reply = client.post("/auth/login", json={
            "email": email or os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
            "password": password,
        })
        assert reply.status_code == 200, f"login failed: {reply.status_code}"
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client


def seed_test_image_and_remix_rights(project_id: str) -> str:
    """Synthetic fixture; does not prove image upload or trusted M12 issuance."""
    created = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        asset = DBAssetVersion(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id,
            project_id=project.id, media_id="synthetic-g72-ci-image",
            version_no=1, sha256="a" * 64, media_type="image",
            content_type="image/png", size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id, created_at=created,
        )
        rights = DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id,
            project_id=project.id, asset_version_id=asset.id,
            status="ALLOWED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="synthetic-nonproduction-fixture",
            captured_by=project.owner_id, captured_at=created,
        )
        db.add_all([asset, rights])
        db.commit()
        return asset.id


def get_result(client: httpx.Client, project_id: str, task_id: str) -> dict:
    reply = client.get(f"/projects/{project_id}/generation-tasks/{task_id}")
    assert reply.status_code == 200, f"task read failed: {reply.status_code}"
    return reply.json()


def check_asset_and_receipts(project_id: str, task_id: str,
                             expected_sha: str | None = None) -> dict:
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.project_id == project_id
        assert task.provider == "deterministic-fake-talking-head"
        assert task.status == "RIGHTS_BLOCKED", (
            f"status={task.status} error={task.error_code}"
        )
        assert task.media_id and task.asset_version_id
        asset = db.get(DBAssetVersion, task.asset_version_id)
        assert asset is not None and asset.project_id == project_id
        assert asset.tenant_id == task.tenant_id and asset.media_id == task.media_id
        assert asset.media_type == "video" and len(asset.sha256) == 64
        assert asset.probe_json["provenance"]["generationTaskId"] == task_id
        events = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
        )).scalars().all()
        assert len(events) == 1, f"ingest events={len(events)}"
        attempts = db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == task_id,
        )).scalar_one()
        assert attempts == 1, f"attempts={attempts}"
        settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        assert settled == 1, f"fake settlement count={settled}"
        media_id, saved_sha = task.media_id, asset.sha256
    response = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{media_id}",
        timeout=30, trust_env=False,
    )
    assert response.status_code == 200, f"sidecar GET={response.status_code}"
    data = response.content
    assert len(data) > 1000 and data[4:8] == b"ftyp", "invalid MP4 bytes"
    digest = hashlib.sha256(data).hexdigest()
    assert digest == saved_sha, "media differs from DB SHA"
    if expected_sha is not None:
        assert digest == expected_sha, "media changed after sidecar restart"
    return {
        "taskId": task_id, "projectId": project_id, "mediaId": media_id,
        "sha256": digest, "bytes": len(data),
        "settledEntries": settled, "ingestEvents": len(events),
        "attempts": attempts,
    }


def prepare() -> None:
    with authenticate() as client:
        response = client.post("/projects", json={
            "name": "FAKE G72 CI network " + uuid.uuid4().hex[:12],
        })
        assert response.status_code == 201, f"project create={response.status_code}"
        project = response.json()
        pid = project["id"]
        asset_id = seed_test_image_and_remix_rights(pid)
        cap_response = client.get("/generation/providers/moneyprinter/capabilities")
        assert cap_response.status_code == 200
        caps = cap_response.json()
        payload = {
            "text": "Synthetic nonproduction network test",
            "durationSeconds": 1, "aspectRatio": "9:16",
            "resolution": "720p", "voiceId": "synthetic-seed",
            "imageAssetVersionId": asset_id,
            "idempotencyKey": str(uuid.uuid4()),
            "expectedProjectRevision": project["revision"],
            "capabilitySnapshotHash": caps["snapshotHash"],
            "confirmSyntheticTestOnly": True,
        }
        base = f"/projects/{pid}/talking-head/fake-generation-tasks"
        preflight = client.post(base + "/validate", json=payload)
        assert preflight.status_code == 200, (
            f"preflight={preflight.status_code} {preflight.text[:200]}"
        )
        response = client.post(base, json=payload)
        assert response.status_code == 202, (
            f"create={response.status_code} {response.text[:200]}"
        )
        task_id = response.json()["taskId"]
        duplicate = client.post(base, json=payload)
        assert duplicate.status_code == 202
        assert duplicate.json()["taskId"] == task_id
        started = time.monotonic()
        last_status = "QUEUED"
        while time.monotonic() - started < 360:
            row = get_result(client, pid, task_id)
            status = row.get("status", "")
            if status != last_status:
                print(json.dumps({"phase": "poll", "status": status}), flush=True)
                last_status = status
            if status == "RIGHTS_BLOCKED":
                break
            if status in {"FAILED", "CANCELED"}:
                raise AssertionError(
                    f"Worker path failed: {status}, {row.get('errorCode')}"
                )
            time.sleep(3)
        else:
            raise AssertionError(f"Worker never finished: {last_status}")
        evidence = check_asset_and_receipts(pid, task_id)
        STATE_PATH.write_text(json.dumps({"evidence": evidence, "replayRequest": payload}), encoding="utf8")
        print(json.dumps({
            "phase": "before_restart", "fakeOnly": True,
            "sourceAsset": "SYNTHETIC_DB_FIXTURE", "result": evidence,
        }), flush=True)



def verify_foreign_tenant_denials(saved: dict, replay_request: dict) -> None:
    """Native nonproduction auth negative, independent of Owner's session.

    A foreign tenant's Owner may not preflight, create, or read the first
    tenant's project/task; neither may the attempt mutate prior receipts.
    The fixture is synthetic, and must never be used against production.
    """
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    now = dt.datetime.now(dt.timezone.utc)
    nonce = uuid.uuid4().hex
    foreign_email = f"ci-g72-foreign-{nonce}@example.invalid"
    foreign_password = "ci-" + uuid.uuid4().hex
    with SessionLocal() as db:
        foreign_tenant = DBTenant(
            id=str(uuid.uuid4()), name="Synthetic foreign G72 tenant",
            slug="ci-g72-foreign-" + nonce,
            project_quota=2, storage_quota_bytes=1024**3,
            used_storage_bytes=0, concurrent_render_quota=1,
            monthly_render_seconds_quota=600, render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"), created_at=now, updated_at=now,
        )
        foreign_owner = DBUser(
            id=str(uuid.uuid4()), tenant_id=foreign_tenant.id,
            email=foreign_email, display_name="Synthetic foreign Owner",
            password_hash=hash_password(foreign_password), role="owner",
            is_active=True, created_at=now, updated_at=now,
        )
        db.add_all([foreign_tenant, foreign_owner])
        db.commit()
    base = f"/projects/{saved['projectId']}/talking-head/fake-generation-tasks"
    with authenticate(email=foreign_email, password_override=foreign_password) as foreign:
        for target in (base + "/validate", base):
            reply = foreign.post(target, json=replay_request)
            assert reply.status_code == 404, (
                f"foreign tenant operation exposed original project: {target} {reply.status_code}"
            )
        for target in (
            f"/projects/{saved['projectId']}",
            f"/projects/{saved['projectId']}/generation-tasks/{saved['taskId']}",
            f"/projects/{saved['projectId']}/generation-tasks/{saved['taskId']}/fake-recovery-check",
            f"/video-use/media/{saved['projectId']}/{saved['mediaId']}",
        ):
            reply = foreign.get(target)
            assert reply.status_code == 404, (
                f"foreign tenant GET exposed original project or task: {target} {reply.status_code}"
            )
        # Range requests must never bypass tenant isolation or leak MP4 bytes.
        ranged = foreign.get(
            f"/video-use/media/{saved['projectId']}/{saved['mediaId']}",
            headers={"Range": "bytes=0-31"},
        )
        assert ranged.status_code == 404, (
            f"foreign tenant ranged media exposed bytes: {ranged.status_code}"
        )
    print(json.dumps({
        "phase": "foreign_tenant_owner_auth",
        "fakeOnly": True, "foreignCreateDenied": True,
        "foreignPreflightDenied": True, "foreignProjectReadDenied": True,
        "foreignTaskReadDenied": True,
        "foreignMediaReadDenied": True,
        "foreignRecoveryCheckDenied": True,
        "foreignRangeReadDenied": True,
    }), flush=True)


def verify() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf8"))
    saved = state["evidence"]
    with authenticate() as client:
        task = get_result(client, saved["projectId"], saved["taskId"])
        assert task["status"] == "RIGHTS_BLOCKED", "replay after restart"
        # Replay after a Worker/sidecar restart must not create another attempt.
        replay = client.post(
            f"/projects/{saved['projectId']}/talking-head/fake-generation-tasks",
            json=state["replayRequest"],
        )
        assert replay.status_code == 202, f"replay={replay.status_code}"
        assert replay.json()["taskId"] == saved["taskId"], "duplicate fake task"
        # Same tenant, separate project: no cross-project task read.
        foreign = client.post("/projects", json={
            "name": "FAKE G72 isolated project " + uuid.uuid4().hex[:12],
        })
        assert foreign.status_code == 201, f"project={foreign.status_code}"
        wrong = client.get(
            f"/projects/{foreign.json()['id']}/generation-tasks/{saved['taskId']}",
        )
        assert wrong.status_code in (403, 404), (
            f"cross-project task visibility={wrong.status_code}"
        )
        verify_foreign_tenant_denials(saved, state["replayRequest"])
        evidence = check_asset_and_receipts(
            saved["projectId"], saved["taskId"], saved["sha256"],
        )
        assert evidence == saved, "asset or receipts mutated after replay"
        media = client.get(
            f"/video-use/media/{saved['projectId']}/{saved['mediaId']}"
        )
        assert media.status_code == 200
        assert hashlib.sha256(media.content).hexdigest() == saved["sha256"]
        print(json.dumps({
            "phase": "after_restart", "fakeOnly": True,
            "verified": True, "result": evidence,
        }), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        verify()
