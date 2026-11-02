"""G70/G71 FAKE-only Compose recovery smoke against the real video-use Sidecar.

The fixture creates an orphan media object through the Sidecar's real upload API,
seeds only synthetic nonproduction DB reconciliation rows, restarts the Sidecar and
Worker in disposable Compose, then exercises the normal Owner recovery API.
It never calls a generation Provider and never grants EXPORT rights.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, "/app")

import httpx
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    DBAssetVersion,
    DBGenerationAttempt,
    DBGenerationEvent,
    DBGenerationTask,
    DBGenerationUsageEntry,
    DBProject,
    DBTenant,
)

G72_STATE_PATH = Path("/tmp/g72-ci-fake-only-network-state.json")
STATE_PATH = Path("/tmp/g70-g71-ci-fake-only-recovery-state.json")
EVIDENCE_PATH = Path("/tmp/g70-g71-ci-fake-only-recovery-evidence.json")
API_BASE = "http://127.0.0.1:8000"
VIDEO_USE_BASE = "http://video-use:8002"


@contextmanager
def authenticate():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    password = os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD")
    assert password and password.startswith("ci-"), "disposable CI credential required"
    with httpx.Client(base_url=API_BASE, timeout=60, trust_env=False) as client:
        reply = client.post(
            "/auth/login",
            json={
                "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
                "password": password,
            },
        )
        assert reply.status_code == 200, f"login failed: {reply.status_code} {reply.text[:200]}"
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client


def write_evidence(payload: dict) -> None:
    EVIDENCE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )


def prepare() -> None:
    assert G72_STATE_PATH.is_file(), "G72 prepare must run first"
    g72 = json.loads(G72_STATE_PATH.read_text(encoding="utf8"))
    project_id = g72["projectId"]
    source_task_id = g72["taskId"]
    source_media_id = g72["mediaId"]

    source_response = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{source_media_id}",
        timeout=60,
        trust_env=False,
    )
    assert source_response.status_code == 200, (
        f"source Sidecar read={source_response.status_code}"
    )
    source_bytes = source_response.content
    assert len(source_bytes) > 1000 and source_bytes[4:8] == b"ftyp"
    digest = hashlib.sha256(source_bytes).hexdigest()

    # Create a second real Sidecar object. The bytes are deterministic FAKE output
    # from the already-passing G72 Worker path, not a real upstream Provider artifact.
    uploaded = httpx.post(
        f"{VIDEO_USE_BASE}/media",
        data={"projectId": project_id},
        files={"file": ("g70-original-orphan.mp4", source_bytes, "video/mp4")},
        timeout=120,
        trust_env=False,
    )
    assert uploaded.status_code == 201, (
        f"orphan Sidecar upload={uploaded.status_code} {uploaded.text[:300]}"
    )
    orphan_media_id = uploaded.json()["mediaId"]
    assert orphan_media_id != source_media_id

    inspected = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{orphan_media_id}/inspect",
        timeout=60,
        trust_env=False,
    )
    assert inspected.status_code == 200, inspected.text[:300]
    inspection = inspected.json()
    assert inspection["projectId"] == project_id
    assert inspection["mediaId"] == orphan_media_id
    assert inspection["contentType"] == "video/mp4"
    assert inspection["sha256"] == digest
    assert inspection["sizeBytes"] == len(source_bytes)
    assert isinstance(inspection.get("metadata", {}).get("video"), dict)

    now = dt.datetime.now(dt.timezone.utc)
    task_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    upstream_job_id = f"fake-original-job-{uuid.uuid4()}"
    provider_artifact_id = f"fake-original-artifact-{uuid.uuid4()}"
    reservation_key = f"g70-compose-recovery-{uuid.uuid4()}"

    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        source_task = db.get(DBGenerationTask, source_task_id)
        assert project is not None and source_task is not None
        assert source_task.project_id == project_id
        assert source_task.tenant_id == project.tenant_id
        assert source_task.requested_by == project.owner_id

        tenant = db.get(DBTenant, project.tenant_id)
        assert tenant is not None
        tenant_id = project.tenant_id
        requested_by = project.owner_id
        storage_before = int(tenant.used_storage_bytes or 0)

        values = {
            column.key: getattr(source_task, column.key)
            for column in DBGenerationTask.__table__.columns
        }
        values.update(
            {
                "id": task_id,
                "tenant_id": project.tenant_id,
                "project_id": project_id,
                "requested_by": project.owner_id,
                "status": "FAILED",
                "progress": 100,
                "message": "Synthetic FAKE-only artifact transfer failure; original Sidecar object retained",
                "request_json": copy.deepcopy(source_task.request_json),
                "request_hash": source_task.request_hash,
                "capability_snapshot_json": copy.deepcopy(source_task.capability_snapshot_json),
                "capability_snapshot_hash": source_task.capability_snapshot_hash,
                "idempotency_key": str(uuid.uuid4()),
                "upstream_job_id": upstream_job_id,
                "provider_artifact_id": provider_artifact_id,
                "media_id": orphan_media_id,
                "asset_version_id": None,
                "attempts": 1,
                "max_attempts": 3,
                "lease_owner": None,
                "lease_epoch": 0,
                "circuit_probe_epoch": None,
                "lease_expires_at": None,
                "cancel_requested_at": None,
                "error_code": "ARTIFACT_INGEST_FAILED",
                "error_message": "synthetic nonproduction transfer failed after Sidecar object persisted",
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": now,
            }
        )
        task = DBGenerationTask(**values)
        attempt = DBGenerationAttempt(
            id=attempt_id,
            generation_task_id=task_id,
            attempt_no=1,
            status="FAILED",
            submission_started_at=now,
            upstream_job_id=upstream_job_id,
            reconciliation_state="HELD",
            error_code="ARTIFACT_INGEST_FAILED",
            error_message="synthetic FAKE-only artifact transfer failure",
            created_at=now,
            completed_at=now,
        )
        failure_event = DBGenerationEvent(
            id=str(uuid.uuid4()),
            generation_task_id=task_id,
            attempt_id=attempt_id,
            event_type="ARTIFACT_INGEST_FAILED",
            from_status="INGESTING",
            to_status="FAILED",
            actor_type="SYSTEM",
            actor_id="g70-g71-compose-fake-only",
            metadata_json={
                "originalUpstreamJobId": upstream_job_id,
                "originalProviderArtifactId": provider_artifact_id,
                "reconciliationState": "HELD",
                "usageReservation": "HELD",
                "orphanedMediaId": orphan_media_id,
                "orphanedMediaSha256": digest,
                "orphanedMediaSizeBytes": len(source_bytes),
                "orphanedMediaMetadata": inspection["metadata"],
                "fixtureKind": "SYNTHETIC_FAKE_ONLY",
            },
            created_at=now,
        )
        reserved_usage = DBGenerationUsageEntry(
            id=str(uuid.uuid4()),
            tenant_id=project.tenant_id,
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            kind="RESERVED",
            request_units=1,
            generated_seconds=1,
            reservation_key=reservation_key,
            config_version_id=None,
            created_at=now,
        )
        db.add_all([task, attempt, failure_event, reserved_usage])
        db.commit()

    state = {
        "taskId": task_id,
        "attemptId": attempt_id,
        "projectId": project_id,
        "tenantId": tenant_id,
        "requestedBy": requested_by,
        "sourceTaskId": source_task_id,
        "sourceMediaId": source_media_id,
        "mediaId": orphan_media_id,
        "sha256": digest,
        "bytes": len(source_bytes),
        "upstreamJobId": upstream_job_id,
        "providerArtifactId": provider_artifact_id,
        "storageBytesBeforeRecovery": storage_before,
        "fixtureKind": "SYNTHETIC_FAKE_ONLY",
        "providerCalls": 0,
    }
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf8",
    )
    write_evidence({"phase": "prepared", **state})
    print(json.dumps({"phase": "g70_g71_orphan_prepared", **state}), flush=True)


def verify() -> None:
    state = json.loads(STATE_PATH.read_text(encoding="utf8"))
    project_id = state["projectId"]
    task_id = state["taskId"]
    media_id = state["mediaId"]
    url = f"/projects/{project_id}/generation-tasks/{task_id}/recover-artifact"

    with authenticate() as client:
        before = client.get(f"/projects/{project_id}/generation-tasks/{task_id}")
        assert before.status_code == 200, before.text[:300]
        assert before.json()["status"] == "FAILED"

        # Generic retry must not create a new paid generation attempt while the
        # original artifact is held for recovery.
        blocked_retry = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/retry"
        )
        assert blocked_retry.status_code == 409, blocked_retry.text[:300]
        assert blocked_retry.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
        with SessionLocal() as db:
            attempt_count = db.execute(
                select(func.count(DBGenerationAttempt.id)).where(
                    DBGenerationAttempt.generation_task_id == task_id
                )
            ).scalar_one()
            assert attempt_count == 1
            assert db.execute(
                select(func.count(DBGenerationUsageEntry.id)).where(
                    DBGenerationUsageEntry.task_id == task_id,
                    DBGenerationUsageEntry.kind == "RESERVED",
                )
            ).scalar_one() == 1
            assert db.execute(
                select(func.count(DBGenerationUsageEntry.id)).where(
                    DBGenerationUsageEntry.task_id == task_id,
                    DBGenerationUsageEntry.kind.in_(("RELEASED", "SETTLED")),
                )
            ).scalar_one() == 0

        recovered = client.post(
            url,
            json={"reason": "Owner verifies original FAKE-only Sidecar orphan after restart"},
        )
        assert recovered.status_code == 200, (
            f"Owner recovery={recovered.status_code} {recovered.text[:500]}"
        )
        payload = recovered.json()
        assert payload["status"] == "RIGHTS_BLOCKED"
        assert payload["results"][0]["mediaId"] == media_id
        asset_version_id = payload["results"][0]["assetVersionId"]

        # Same Owner may repeat the same recovery request; it must be idempotent.
        repeated = client.post(
            url,
            json={"reason": "Owner repeats recovery to verify idempotency after restart"},
        )
        assert repeated.status_code == 200, repeated.text[:300]
        assert repeated.json()["results"][0]["assetVersionId"] == asset_version_id

        # Storage availability is not an M12 EXPORT grant.
        denied = client.get(f"/video-use/media/{project_id}/{media_id}")
        assert denied.status_code == 403, denied.text[:300]
        assert denied.json()["detail"]["code"] == "RIGHTS_MISSING"

    inspection_response = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{media_id}/inspect",
        timeout=60,
        trust_env=False,
    )
    assert inspection_response.status_code == 200, inspection_response.text[:300]
    inspection = inspection_response.json()
    assert inspection["sha256"] == state["sha256"]
    assert inspection["sizeBytes"] == state["bytes"]

    direct_media = httpx.get(
        f"{VIDEO_USE_BASE}/media/{project_id}/{media_id}",
        timeout=60,
        trust_env=False,
    )
    assert direct_media.status_code == 200
    assert len(direct_media.content) == state["bytes"]
    assert hashlib.sha256(direct_media.content).hexdigest() == state["sha256"]

    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        project = db.get(DBProject, project_id)
        tenant = db.get(DBTenant, state["tenantId"])
        attempt = db.get(DBGenerationAttempt, state["attemptId"])
        assert task is not None and project is not None and tenant is not None
        assert attempt is not None
        assert task.status == "RIGHTS_BLOCKED"
        assert task.error_code is None
        assert task.asset_version_id == asset_version_id
        assert task.media_id == media_id
        assert task.upstream_job_id == state["upstreamJobId"]
        assert task.provider_artifact_id == state["providerArtifactId"]
        assert task.attempts == 1
        assert attempt.reconciliation_state == "ARTIFACT_INGESTED"

        attempts = db.execute(
            select(func.count(DBGenerationAttempt.id)).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        assert attempts == 1

        reserved = db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "RESERVED",
            )
        ).scalar_one()
        released = db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "RELEASED",
            )
        ).scalar_one()
        settled = db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )
        ).scalar_one()
        assert (reserved, released, settled) == (1, 0, 0)

        failure_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
            )
        ).scalar_one()
        recovery_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        assert (failure_events, recovery_events) == (1, 1)

        asset = db.get(DBAssetVersion, asset_version_id)
        assert asset is not None
        assert asset.project_id == project_id and asset.tenant_id == state["tenantId"]
        assert asset.media_id == media_id
        assert asset.sha256 == state["sha256"]
        assert asset.size_bytes == state["bytes"]
        assert asset.created_by == state["requestedBy"]

        material_matches = [
            item for item in (project.materials or [])
            if isinstance(item, dict) and item.get("id") == media_id
        ]
        assert len(material_matches) == 1
        assert int(tenant.used_storage_bytes) == (
            int(state["storageBytesBeforeRecovery"]) + int(state["bytes"])
        )

        receipt = db.execute(
            select(DBGenerationEvent).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        assert receipt.actor_type == "OWNER"
        assert receipt.actor_id == state["requestedBy"]
        assert receipt.metadata_json["usageReservationState"] == "RESERVED_HELD"
        assert receipt.metadata_json["m11M13Reconciliation"] == "PENDING"
        assert receipt.metadata_json["sha256"] == state["sha256"]
        assert receipt.metadata_json["sizeBytes"] == state["bytes"]

    result = {
        **state,
        "verified": True,
        "assetVersionId": asset_version_id,
        "attemptRows": attempts,
        "reservedEntries": reserved,
        "releasedEntries": released,
        "settledEntries": settled,
        "failureEvents": failure_events,
        "recoveryEvents": recovery_events,
        "storageBytesAfterRecovery": state["storageBytesBeforeRecovery"] + state["bytes"],
        "publicExport": "403 RIGHTS_MISSING",
        "sidecarInspectAfterRestart": "PASS",
        "sidecarBytesAfterRestart": "PASS",
        "genericRetry": "409 ARTIFACT_RECOVERY_REQUIRED; no new attempt",
        "ownerRecovery": "PASS",
        "repeatedOwnerRecovery": "IDEMPOTENT",
        "providerCalls": 0,
    }
    write_evidence({"phase": "verified", **result})
    print(json.dumps({"phase": "g70_g71_recovery_verified", **result}), flush=True)
    verify_missing_original_object()


def verify_missing_original_object() -> None:
    """The absent-original-object path must remain HELD and never regenerate."""
    state = json.loads(STATE_PATH.read_text(encoding="utf8"))
    project_id = state["projectId"]
    missing_media_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    upstream_job_id = f"fake-missing-original-job-{uuid.uuid4()}"
    provider_artifact_id = f"fake-missing-original-artifact-{uuid.uuid4()}"
    reservation_key = f"g70-missing-original-{uuid.uuid4()}"
    now = dt.datetime.now(dt.timezone.utc)
    missing_sha256 = "b" * 64
    missing_size = int(state["bytes"])

    with SessionLocal() as db:
        source_task = db.get(DBGenerationTask, state["sourceTaskId"])
        project = db.get(DBProject, project_id)
        tenant = db.get(DBTenant, state["tenantId"])
        assert source_task is not None and project is not None and tenant is not None
        assert source_task.project_id == project_id
        assert source_task.tenant_id == state["tenantId"]
        assert source_task.requested_by == state["requestedBy"]
        storage_before = int(tenant.used_storage_bytes or 0)

        values = {
            column.key: getattr(source_task, column.key)
            for column in DBGenerationTask.__table__.columns
        }
        values.update({
            "id": task_id,
            "tenant_id": state["tenantId"],
            "project_id": project_id,
            "requested_by": state["requestedBy"],
            "status": "FAILED",
            "progress": 100,
            "message": "Synthetic FAKE-only original Sidecar object absent after transfer failure",
            "request_json": copy.deepcopy(source_task.request_json),
            "request_hash": source_task.request_hash,
            "capability_snapshot_json": copy.deepcopy(source_task.capability_snapshot_json),
            "capability_snapshot_hash": source_task.capability_snapshot_hash,
            "idempotency_key": str(uuid.uuid4()),
            "upstream_job_id": upstream_job_id,
            "provider_artifact_id": provider_artifact_id,
            "media_id": missing_media_id,
            "asset_version_id": None,
            "attempts": 1,
            "max_attempts": 3,
            "lease_owner": None,
            "lease_epoch": 0,
            "circuit_probe_epoch": None,
            "lease_expires_at": None,
            "cancel_requested_at": None,
            "error_code": "ARTIFACT_INGEST_FAILED",
            "error_message": "synthetic missing Sidecar object; no retrieval permission",
            "created_at": now,
            "updated_at": now,
            "started_at": now,
            "completed_at": now,
        })
        task = DBGenerationTask(**values)
        attempt = DBGenerationAttempt(
            id=attempt_id,
            generation_task_id=task_id,
            attempt_no=1,
            status="FAILED",
            submission_started_at=now,
            upstream_job_id=upstream_job_id,
            reconciliation_state="HELD",
            error_code="ARTIFACT_INGEST_FAILED",
            error_message="synthetic missing original Sidecar object",
            created_at=now,
            completed_at=now,
        )
        failure_event = DBGenerationEvent(
            id=str(uuid.uuid4()),
            generation_task_id=task_id,
            attempt_id=attempt_id,
            event_type="ARTIFACT_INGEST_FAILED",
            from_status="INGESTING",
            to_status="FAILED",
            actor_type="SYSTEM",
            actor_id="g70-g71-missing-original-fake-only",
            metadata_json={
                "originalUpstreamJobId": upstream_job_id,
                "originalProviderArtifactId": provider_artifact_id,
                "reconciliationState": "HELD",
                "usageReservation": "HELD",
                "orphanedMediaId": missing_media_id,
                "orphanedMediaSha256": missing_sha256,
                "orphanedMediaSizeBytes": missing_size,
                "orphanedMediaMetadata": {
                    "video": {"width": 320, "height": 568},
                    "durationSeconds": 1.0,
                },
                "fixtureKind": "SYNTHETIC_FAKE_ONLY_MISSING_OBJECT",
            },
            created_at=now,
        )
        reserved_usage = DBGenerationUsageEntry(
            id=str(uuid.uuid4()),
            tenant_id=state["tenantId"],
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            kind="RESERVED",
            request_units=1,
            generated_seconds=1,
            reservation_key=reservation_key,
            config_version_id=None,
            created_at=now,
        )
        db.add_all([task, attempt, failure_event, reserved_usage])
        db.commit()

    with authenticate() as client:
        blocked_retry = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/retry"
        )
        assert blocked_retry.status_code == 409, blocked_retry.text[:300]
        assert blocked_retry.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"

        denied_recovery = client.post(
            f"/projects/{project_id}/generation-tasks/{task_id}/recover-artifact",
            json={"reason": "Owner verifies missing original Sidecar object"},
        )
        assert denied_recovery.status_code == 409, denied_recovery.text[:500]
        assert denied_recovery.json()["detail"]["code"] == "ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED"

        inspect = httpx.get(
            f"{VIDEO_USE_BASE}/media/{project_id}/{missing_media_id}/inspect",
            timeout=30,
            trust_env=False,
        )
        assert inspect.status_code == 404, f"missing Sidecar inspect={inspect.status_code}"
        direct = httpx.get(
            f"{VIDEO_USE_BASE}/media/{project_id}/{missing_media_id}",
            timeout=30,
            trust_env=False,
        )
        assert direct.status_code == 404, f"missing Sidecar read={direct.status_code}"

    with SessionLocal() as db:
        task = db.get(DBGenerationTask, task_id)
        attempt = db.get(DBGenerationAttempt, attempt_id)
        tenant = db.get(DBTenant, state["tenantId"])
        assert task is not None and attempt is not None and tenant is not None
        assert task.status == "FAILED"
        assert task.error_code == "ARTIFACT_INGEST_FAILED"
        assert task.asset_version_id is None
        assert task.media_id == missing_media_id
        assert task.attempts == 1
        assert attempt.reconciliation_state == "HELD"
        assert int(tenant.used_storage_bytes or 0) == storage_before

        attempt_count = db.execute(
            select(func.count(DBGenerationAttempt.id)).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        reserved = db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "RESERVED",
            )
        ).scalar_one()
        released_or_settled = db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind.in_(("RELEASED", "SETTLED")),
            )
        ).scalar_one()
        failure_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
            )
        ).scalar_one()
        recovery_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        assert (attempt_count, reserved, released_or_settled) == (1, 1, 0)
        assert (failure_events, recovery_events) == (1, 0)
        assert not any(
            isinstance(item, dict) and item.get("id") == missing_media_id
            for item in (db.get(DBProject, project_id).materials or [])
        )

    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf8"))
    evidence["missingOriginalObject"] = {
        "taskId": task_id,
        "mediaId": missing_media_id,
        "sha256Evidence": "synthetic-valid-format-only",
        "expectedResult": "409 ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED",
        "genericRetry": "409 ARTIFACT_RECOVERY_REQUIRED; no new attempt",
        "taskRemains": "FAILED / ARTIFACT_INGEST_FAILED",
        "attempts": 1,
        "reconciliation": "HELD",
        "usage": {"RESERVED": 1, "RELEASED": 0, "SETTLED": 0},
        "assetVersionCreated": False,
        "projectMaterialCreated": False,
        "storageQuotaChanged": False,
        "sidecarInspect": "404",
        "sidecarRead": "404",
        "providerCalls": 0,
        "verified": True,
    }
    write_evidence(evidence)
    print(json.dumps({
        "phase": "g70_g71_missing_original_fail_closed_verified",
        **evidence["missingOriginalObject"],
    }), flush=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "verify"))
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare()
    else:
        verify()
