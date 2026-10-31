"""Real Worker-path G70/G71 fault injection in disposable deterministic-FAKE Compose.

The test drives the normal G72 API entry and Worker. A test-only proxy injects
three bounded failures at the API -> video-use POST /media boundary:
  1) Sidecar commits but all three acknowledgements are lost (idempotent recovery).
  2) All three requests fail before reaching Sidecar (no-object fail-closed).

No real Provider, credential, production service, or paid retry is used.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

import httpx
from sqlalchemy import func, select
sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models import (
    DBAssetVersion,
    DBGenerationAttempt,
    DBGenerationEvent,
    DBGenerationTask,
    DBGenerationUsageEntry,
    DBProject,
    DBRightsSnapshot,
    DBTenant,
)

API_BASE = "http://127.0.0.1:8000"
PROXY_BASE = "http://g70-fault-proxy:8003"
VIDEO_USE_BASE = "http://video-use:8002"
TOKEN = os.environ.get("G70_FAULT_PROXY_TOKEN", "")
EVIDENCE_PATH = Path("/tmp/g70-g71-worker-transfer-failure-evidence.json")


@contextmanager
def authenticate():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert TOKEN, "ephemeral FAKE-only proxy token is required"
    password = os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD")
    assert password and password.startswith("ci-"), "disposable CI credential required"
    with httpx.Client(base_url=API_BASE, timeout=45, trust_env=False) as client:
        reply = client.post("/auth/login", json={
            "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
            "password": password,
        })
        assert reply.status_code == 200, f"login failed: {reply.status_code} {reply.text[:200]}"
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client


def seed_test_image_and_remix_rights(project_id: str) -> str:
    """Synthetic DB-only image/right fixture; not trusted M12 evidence."""
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        asset = DBAssetVersion(
            id=str(uuid.uuid4()),
            tenant_id=project.tenant_id,
            project_id=project.id,
            media_id="synthetic-g70-fake-image",
            version_no=1,
            sha256="a" * 64,
            media_type="image",
            content_type="image/png",
            size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id,
            created_at=now,
        )
        rights = DBRightsSnapshot(
            id=str(uuid.uuid4()),
            tenant_id=project.tenant_id,
            project_id=project.id,
            asset_version_id=asset.id,
            status="ALLOWED",
            purpose="REMIX",
            territory="TEST",
            valid_from=None,
            valid_until=None,
            evidence_ref="synthetic-g70-fake-only-fixture",
            captured_by=project.owner_id,
            captured_at=now,
        )
        db.add_all([asset, rights])
        db.commit()
        return asset.id


def proxy_request(method: str, path: str, **kwargs) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}))
    headers["X-G70-Fault-Token"] = TOKEN
    with httpx.Client(timeout=45, trust_env=False) as client:
        return client.request(method, f"{PROXY_BASE}{path}", headers=headers, **kwargs)


def proxy_stats() -> dict:
    response = proxy_request("GET", "/__fault/stats")
    assert response.status_code == 200, response.text[:300]
    return response.json()


def arm_proxy(mode: str) -> dict:
    response = proxy_request(
        "POST",
        "/__fault/arm",
        json={"mode": mode, "failures": 3},
    )
    assert response.status_code == 200, response.text[:300]
    return response.json()


def expected_media_id(project_id: str, task_id: str) -> str:
    key = f"generation:{task_id}"
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"aether-video-use:{project_id}:{key}",
    ))


def create_fake_task(client: httpx.Client, mode: str) -> tuple[str, str, int]:
    project_response = client.post("/projects", json={
        "name": f"G70 worker transfer {mode} {uuid.uuid4().hex[:10]}",
    })
    assert project_response.status_code == 201, project_response.text[:300]
    project = project_response.json()
    project_id = project["id"]
    asset_id = seed_test_image_and_remix_rights(project_id)

    capabilities = client.get("/generation/providers/moneyprinter/capabilities")
    assert capabilities.status_code == 200, capabilities.text[:300]
    caps = capabilities.json()
    payload = {
        "text": "Synthetic G70/G71 worker transfer failure test",
        "durationSeconds": 1,
        "aspectRatio": "9:16",
        "resolution": "720p",
        "voiceId": "synthetic-g70-seed",
        "imageAssetVersionId": asset_id,
        "idempotencyKey": str(uuid.uuid4()),
        "expectedProjectRevision": project["revision"],
        "capabilitySnapshotHash": caps["snapshotHash"],
        "confirmSyntheticTestOnly": True,
    }
    base = f"/projects/{project_id}/talking-head/fake-generation-tasks"
    preflight = client.post(base + "/validate", json=payload)
    assert preflight.status_code == 200, (
        f"{mode} preflight={preflight.status_code} {preflight.text[:300]}"
    )

    with SessionLocal() as db:
        project_row = db.get(DBProject, project_id)
        tenant = db.get(DBTenant, project_row.tenant_id)
        assert tenant is not None
        storage_before = int(tenant.used_storage_bytes or 0)

    # Arm only after all setup/preflight work; the next POST /media must be
    # the real Worker -> API artifact-intake upload for this task.
    arm_proxy(mode)
    created = client.post(base, json=payload)
    assert created.status_code == 202, f"{mode} create={created.status_code} {created.text[:300]}"
    task_id = created.json()["taskId"]
    return project_id, task_id, storage_before


def poll_failed(client: httpx.Client, project_id: str, task_id: str, mode: str) -> dict:
    started = time.monotonic()
    last_status = "QUEUED"
    while time.monotonic() - started < 360:
        response = client.get(f"/projects/{project_id}/generation-tasks/{task_id}")
        assert response.status_code == 200, response.text[:300]
        row = response.json()
        status = row.get("status")
        if status != last_status:
            print(json.dumps({
                "phase": "worker_task_poll",
                "mode": mode,
                "taskId": task_id,
                "status": status,
                "errorCode": row.get("errorCode"),
            }), flush=True)
            last_status = status
        if status == "FAILED":
            return row
        if status in {"RIGHTS_BLOCKED", "CANCELED", "UNKNOWN"}:
            raise AssertionError(f"{mode} unexpected terminal state: {row}")
        time.sleep(1)
    raise AssertionError(f"{mode} Worker did not finish; last status={last_status}")


def inspect_task(project_id: str, task_id: str, storage_before: int) -> dict:
    media_id = expected_media_id(project_id, task_id)
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        project = db.get(DBProject, project_id)
        assert task is not None and project is not None
        attempt = db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        tenant = db.get(DBTenant, task.tenant_id)
        assert tenant is not None
        events = db.execute(
            select(DBGenerationEvent).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
            )
        ).scalars().all()
        assert len(events) == 1
        metadata = events[0].metadata_json or {}
        counts = {
            kind: db.execute(
                select(func.count(DBGenerationUsageEntry.id)).where(
                    DBGenerationUsageEntry.task_id == task_id,
                    DBGenerationUsageEntry.kind == kind,
                )
            ).scalar_one()
            for kind in ("RESERVED", "RELEASED", "SETTLED")
        }
        attempt_count = db.execute(
            select(func.count(DBGenerationAttempt.id)).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        assert task.provider == "deterministic-fake-talking-head"
        assert task.status == "FAILED"
        assert task.error_code == "ARTIFACT_INGEST_FAILED"
        assert task.upstream_job_id
        assert task.provider_artifact_id
        assert task.media_id == media_id
        assert task.asset_version_id is None
        assert attempt.status == "FAILED"
        assert attempt.error_code == "ARTIFACT_INGEST_FAILED"
        assert attempt.reconciliation_state == "HELD"
        assert attempt.upstream_job_id == task.upstream_job_id
        assert attempt_count == 1
        assert counts == {"RESERVED": 1, "RELEASED": 0, "SETTLED": 0}
        assert metadata.get("originalUpstreamJobId") == task.upstream_job_id
        assert metadata.get("originalProviderArtifactId") == task.provider_artifact_id
        assert metadata.get("orphanedMediaId") == media_id
        assert metadata.get("reconciliationState") == "HELD"
        assert metadata.get("usageReservation") == "HELD"
        assert len(metadata.get("orphanedMediaSha256", "")) == 64
        assert int(metadata.get("orphanedMediaSizeBytes") or 0) > 0
        assert int(tenant.used_storage_bytes or 0) == storage_before
        return {
            "taskId": task_id,
            "projectId": project_id,
            "tenantId": task.tenant_id,
            "taskStatus": task.status,
            "errorCode": task.error_code,
            "attemptId": attempt.id,
            "attempts": attempt_count,
            "upstreamJobId": task.upstream_job_id,
            "providerArtifactId": task.provider_artifact_id,
            "mediaId": media_id,
            "sha256": metadata["orphanedMediaSha256"],
            "sizeBytes": int(metadata["orphanedMediaSizeBytes"]),
            "usage": counts,
            "reconciliation": attempt.reconciliation_state,
            "storageBytesBefore": storage_before,
            "storageBytesAfterFailure": int(tenant.used_storage_bytes or 0),
            "failureEventCount": len(events),
            "fixtureKind": "DETERMINISTIC_FAKE_WORKER_PATH",
            "providerCalls": 0,
        }


def run_case(client: httpx.Client, mode: str) -> dict:
    project_id, task_id, storage_before = create_fake_task(client, mode)
    failed_row = poll_failed(client, project_id, task_id, mode)
    assert failed_row.get("errorCode") == "ARTIFACT_INGEST_FAILED", failed_row
    evidence = inspect_task(project_id, task_id, storage_before)

    # A generic retry must never create another generation attempt or paid job.
    retry = client.post(f"/projects/{project_id}/generation-tasks/{task_id}/retry")
    assert retry.status_code == 409, retry.text[:300]
    assert retry.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"

    media_id = evidence["mediaId"]
    inspection = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{media_id}/inspect",
        timeout=45,
        trust_env=False,
    )

    if mode == "forward_then_drop_ack":
        assert inspection.status_code == 200, inspection.text[:300]
        inspected = inspection.json()
        assert inspected["sha256"] == evidence["sha256"]
        assert inspected["sizeBytes"] == evidence["sizeBytes"]
        media = httpx.get(
            f"{VIDEO_USE_BASE}/media/{project_id}/{media_id}",
            timeout=45,
            trust_env=False,
        )
        assert media.status_code == 200
        assert hashlib.sha256(media.content).hexdigest() == evidence["sha256"]
        assert len(media.content) == evidence["sizeBytes"]

        recovery = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/recover-artifact",
            json={"reason": "Owner verifies the original Sidecar object after a lost FAKE-only acknowledgement"},
        )
        assert recovery.status_code == 200, recovery.text[:500]
        recovered = recovery.json()
        assert recovered["status"] == "RIGHTS_BLOCKED"
        asset_version_id = recovered["results"][0]["assetVersionId"]

        repeated = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/recover-artifact",
            json={"reason": "Owner repeats the same FAKE-only orphan recovery to verify idempotency"},
        )
        assert repeated.status_code == 200, repeated.text[:300]
        assert repeated.json()["results"][0]["assetVersionId"] == asset_version_id

        public_export = client.get(f"/video-use/media/{project_id}/{media_id}")
        assert public_export.status_code == 403, public_export.text[:300]
        assert public_export.json()["detail"]["code"] == "RIGHTS_MISSING"

        with SessionLocal() as db:
            task = db.get(DBGenerationTask, task_id)
            attempt = db.execute(
                select(DBGenerationAttempt).where(
                    DBGenerationAttempt.generation_task_id == task_id
                )
            ).scalar_one()
            tenant = db.get(DBTenant, task.tenant_id)
            asset = db.get(DBAssetVersion, asset_version_id)
            failure_count = db.execute(
                select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
                )
            ).scalar_one()
            recovery_count = db.execute(
                select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
                )
            ).scalar_one()
            counts = {
                kind: db.execute(
                    select(func.count(DBGenerationUsageEntry.id)).where(
                        DBGenerationUsageEntry.task_id == task_id,
                        DBGenerationUsageEntry.kind == kind,
                    )
                ).scalar_one()
                for kind in ("RESERVED", "RELEASED", "SETTLED")
            }
            assert task.status == "RIGHTS_BLOCKED"
            assert task.error_code is None
            assert task.media_id == media_id
            assert task.asset_version_id == asset_version_id
            assert task.upstream_job_id == evidence["upstreamJobId"]
            assert task.provider_artifact_id == evidence["providerArtifactId"]
            assert attempt.reconciliation_state == "ARTIFACT_INGESTED"
            assert attempt.status == "RIGHTS_BLOCKED"
            assert counts == {"RESERVED": 1, "RELEASED": 0, "SETTLED": 0}
            assert (failure_count, recovery_count) == (1, 1)
            assert asset is not None
            assert asset.media_id == media_id
            assert asset.sha256 == evidence["sha256"]
            assert asset.size_bytes == evidence["sizeBytes"]
            assert int(tenant.used_storage_bytes or 0) == (
                storage_before + evidence["sizeBytes"]
            )
        evidence["manualRecovery"] = {
            "result": "PASS",
            "status": "RIGHTS_BLOCKED",
            "assetVersionId": asset_version_id,
            "repeatedRecovery": "IDEMPOTENT",
            "publicExport": "403 RIGHTS_MISSING",
            "usage": counts,
            "reconciliation": "ARTIFACT_INGESTED",
        }
    else:
        assert inspection.status_code == 404, f"pre-forward failure unexpectedly stored media: {inspection.status_code}"
        recovery = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/recover-artifact",
            json={"reason": "Owner verifies that no Sidecar object exists after the FAKE-only pre-storage failure"},
        )
        assert recovery.status_code == 409, recovery.text[:300]
        assert recovery.json()["detail"]["code"] == "ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED"
        with SessionLocal() as db:
            task = db.get(DBGenerationTask, task_id)
            attempt = db.execute(
                select(DBGenerationAttempt).where(
                    DBGenerationAttempt.generation_task_id == task_id
                )
            ).scalar_one()
            tenant = db.get(DBTenant, task.tenant_id)
            recovery_count = db.execute(
                select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
                )
            ).scalar_one()
            assert task.status == "FAILED"
            assert task.error_code == "ARTIFACT_INGEST_FAILED"
            assert task.asset_version_id is None
            assert attempt.reconciliation_state == "HELD"
            assert recovery_count == 0
            assert int(tenant.used_storage_bytes or 0) == storage_before
        evidence["manualRecovery"] = {
            "result": "DENY_ONLY_PASS",
            "status": "409 ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED",
            "usage": evidence["usage"],
            "reconciliation": "HELD",
        }

    evidence["proxy"] = proxy_stats()
    return evidence


def main() -> None:
    with authenticate() as client:
        committed_orphan = run_case(client, "forward_then_drop_ack")
        pre_storage_failure = run_case(client, "fail_before_forward")

    proxy = proxy_stats()
    committed = committed_orphan["proxy"]
    pre_storage = pre_storage_failure["proxy"]
    assert committed["mode"] == "forward_then_drop_ack"
    assert committed["arm_attempts"] == 3
    assert committed["arm_forwarded"] == 3
    assert committed["arm_dropped_success"] == 3
    assert committed["arm_upstream_statuses"] == [201, 201, 201]
    assert committed["arm_replay_flags"] == [False, True, True]
    assert len(set(committed["arm_media_ids"])) == 1
    assert committed["arm_media_ids"][0] == committed_orphan["mediaId"]
    assert committed["arm_replay_flags"] == [False, True, True]

    assert pre_storage["mode"] == "fail_before_forward"
    assert pre_storage["arm_attempts"] == 3
    assert pre_storage["arm_forwarded"] == 0
    assert pre_storage["arm_dropped_success"] == 0
    assert pre_storage["arm_upstream_statuses"] == []
    assert pre_storage["arm_media_ids"] == []
    assert pre_storage["arm_replay_flags"] == []
    assert pre_storage["arm_replay_flags"] == []
    assert proxy["armed"] is False and proxy["remaining"] == 0

    evidence = {
        "phase": "g70_g71_real_worker_transfer_failure_verified",
        "headScope": "current PR branch CI checkout; exact SHA is recorded by the workflow run",
        "environment": {
            "runtime": "disposable Docker Compose",
            "stage": "nonproduction",
            "providerMode": "deterministic-fake",
            "realProviderCalls": 0,
        },
        "command": "python /tmp/g70-g71-worker-transfer-failure-smoke.py",
        "adapterRetries": 2,
        "requestsPerFaultPlan": 3,
        "commitThenLostAck": committed_orphan,
        "preStorageFailure": pre_storage_failure,
        "conclusion": {
            "lostAck": "one deterministic Sidecar media ID survives three lost acknowledgements; Owner recovery succeeds without a new generation attempt",
            "preStorageFailure": "no Sidecar object exists; task and usage remain HELD; Owner recovery fails closed pending original artifact retrieval",
            "automaticPaidRetry": "none",
            "providerCalls": 0,
        },
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
