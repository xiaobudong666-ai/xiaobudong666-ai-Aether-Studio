"""G76 PostgreSQL-only concurrency proof. FAKE-only; no Provider calls.

The ordinary API/Compose suite uses SQLite and therefore cannot prove that
PostgreSQL SELECT FOR UPDATE serializes lease validation with task mutation.
This test uses a disposable CI PostgreSQL service and verifies the actual
heartbeat endpoint waits for the task-row lock, then observes the new lease.
"""
from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier, Event, Lock, get_ident
from unittest.mock import MagicMock

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.auth import hash_password
from app.database import Base, build_engine
from app.main import create_app
from app.video_use_adapter import VideoUseError
from app.models import (
    DBAssetVersion,
    DBDirectMediaUploadHold,
    DBGenerationAttempt,
    DBGenerationCircuitState,
    DBGenerationEvent,
    DBGenerationProviderEvent,
    DBGenerationTask,
    DBGenerationUsageEntry,
    DBProject,
    DBRightsSnapshot,
    DBTenant,
    DBUser,
)


OWNER_PASSWORD = secrets.token_urlsafe(24)
WORKER_TOKEN = secrets.token_urlsafe(32)


def _expected_media_id(project_id: str, idempotency_key: str) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"aether-video-use:{project_id}:{idempotency_key}",
    ))


class FakeMediaStore:
    def __init__(self):
        self.media_objects = {}

    def check_health(self):
        return {"status": "healthy"}

    def get_capabilities(self):
        return {"render": True}

    def upload_media(
        self, project_id, filename, content_type, stream, *,
        idempotency_key=None, content_sha256=None,
    ):
        payload = stream.read()
        digest = hashlib.sha256(payload).hexdigest()
        if (idempotency_key is None) != (content_sha256 is None):
            raise ValueError("idempotency key and content SHA-256 must be supplied together")
        if content_sha256 is not None and digest != content_sha256.lower():
            raise ValueError("uploaded bytes do not match the supplied content SHA-256")
        media_id = (
            _expected_media_id(project_id, idempotency_key)
            if idempotency_key is not None
            else f"unused-{uuid.uuid4()}"
        )
        self.media_objects[(project_id, media_id)] = payload
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            # Mirror the real Sidecar classification; keep production strict.
            "mediaType": "video",
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(payload),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }
    def inspect_media(self, project_id, media_id):
        stored = self.media_objects.get((project_id, media_id))
        if stored is None:
            raise VideoUseError("FAKE media not found")
        return {"projectId": project_id, "mediaId": media_id,
                "sizeBytes": len(stored),
                "sha256": hashlib.sha256(stored).hexdigest()}


@pytest.fixture()
def postgres_context(monkeypatch):
    database_url = os.environ.get("AETHER_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("AETHER_TEST_POSTGRES_URL is only set by the disposable PostgreSQL CI job")

    monkeypatch.setenv("AETHER_MAX_UPLOAD_BYTES", "1024")
    engine = build_engine(database_url)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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
        video_use_adapter=FakeMediaStore(),
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="g76-postgres-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    app.state.moneyprinter = MagicMock()

    try:
        with TestClient(app) as client:
            login = client.post(
                "/auth/login",
                json={
                    "email": "g76-postgres-owner@example.com",
                    "password": OWNER_PASSWORD,
                },
            )
            assert login.status_code == 200, login.text
            client.headers.update({"X-Aether-CSRF": "1"})
            yield client, sessions, app
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()



def test_postgres_health_endpoint_uses_actual_database_dialect(postgres_context):
    """Disposable native PostgreSQL readiness must not execute SQLite PRAGMA.

    FAKE-only API and no real Provider call; SQLite WAL behavior is covered
    independently in test_main.py.
    """
    client, _, app = postgres_context
    response = client.get("/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "api"
    assert body["database"] == "postgresql"
    assert body["journal_mode"] is None
    assert type(body["setupRequired"]) is bool
    assert type(body["timestamp"]) is str and body["timestamp"]
    app.state.moneyprinter.assert_not_called()


def _create_task(client: TestClient) -> str:
    project_response = client.post("/projects", json={"name": "G76 PostgreSQL lock proof"})
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    capabilities = client.get("/generation/providers/moneyprinter/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"g76-postgres-lock-{uuid.uuid4()}"))
    payload = {
        "videoSubject": "A synthetic FAKE-only concurrency test",
        "videoAspect": "9:16",
        "voiceName": "en-US-JennyNeural",
        "videoConcatMode": "random",
        "videoClipDuration": 5,
        "outputCount": 1,
        "inputAssetVersionIds": [],
        "idempotencyKey": key,
        "capabilitySnapshotHash": capabilities.json()["snapshotHash"],
        "expectedProjectRevision": project["revision"],
        "confirmExternalGeneration": True,
    }
    path = f"/projects/{project['id']}/generation-tasks"
    response = client.post(path, headers={"Idempotency-Key": key}, json=payload)
    # An optimistic capability snapshot may go stale while other synthetic
    # usage entries are created or the rolling window advances. Retry ONLY the
    # documented stale-hash 409; preserve every other error as a test failure.
    for _ in range(4):
        if response.status_code == 202:
            break
        detail = response.json().get("detail", {})
        if response.status_code != 409 or detail.get("code") not in {
            "CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED",
        }:
            break
        capabilities = client.get("/generation/providers/moneyprinter/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        payload["capabilitySnapshotHash"] = capabilities.json()["snapshotHash"]
        preflight = client.post(path + "/validate", json=payload)
        if preflight.status_code == 200:
            payload["capabilitySnapshotHash"] = preflight.json()["capabilitySnapshotHash"]
        elif (
            preflight.status_code != 409
            or preflight.json().get("detail", {}).get("code") not in {
                "CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED",
            }
        ):
            assert preflight.status_code == 200, preflight.text
        response = client.post(path, headers={"Idempotency-Key": key}, json=payload)
    assert response.status_code == 202, response.text
    return response.json()["taskId"]



def _g72_create_claimed_typed_fake_source(
    client, sessions, worker_id: str, *, source_sha: str,
    valid_until: datetime.datetime | None = None,
    claim_after_create: bool = True,
):
    """Real typed FAKE creation/claim on disposable PG, not a converted paid task.

    This preserves append-only authenticated TASK_CREATED evidence; the two
    upload-race tests must not rewrite the creation log or bypass its guard.
    """
    project_response = client.post(
        "/projects", json={"name": "G72 PG native source " + str(uuid.uuid4())},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    with sessions() as db:
        row = db.get(DBProject, project["id"])
        assert row is not None
        now = datetime.datetime.now(datetime.timezone.utc)
        source_id = str(uuid.uuid4())
        tenant_id, requested_by = row.tenant_id, row.owner_id
        db.add(DBAssetVersion(
            id=source_id, tenant_id=tenant_id, project_id=row.id,
            media_id="g72-fake-source-" + source_id, version_no=1,
            sha256=source_sha, media_type="image", content_type="image/png",
            size_bytes=53, probe_json={"image": {"width": 16, "height": 16}},
            created_by=requested_by, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=tenant_id, project_id=row.id,
            asset_version_id=source_id, status="ALLOWED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=valid_until,
            evidence_ref="g72-typed-fake-disposable-only",
            captured_by=requested_by, captured_at=now,
        ))
        db.commit()

    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    payload = {
        "text": "synthetic PG concurrency proof", "durationSeconds": 1,
        "aspectRatio": "9:16", "resolution": "720p",
        "voiceId": "synthetic-seed", "imageAssetVersionId": source_id,
        "idempotencyKey": str(uuid.uuid4()),
        "expectedProjectRevision": project["revision"],
        "confirmSyntheticTestOnly": True,
    }
    created = None
    stale = {"CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED"}
    for _ in range(4):
        capabilities = client.get("/generation/providers/moneyprinter/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        payload["capabilitySnapshotHash"] = capabilities.json()["snapshotHash"]
        preflight = client.post(path + "/validate", json=payload)
        if preflight.status_code != 200:
            if preflight.json().get("detail", {}).get("code") in stale:
                continue
            pytest.fail("Typed G72 PG preflight: " + preflight.text)
        payload["capabilitySnapshotHash"] = preflight.json()["capabilitySnapshotHash"]
        created = client.post(path, json=payload)
        if created.status_code == 202:
            break
        if created.json().get("detail", {}).get("code") not in stale:
            break
    assert created is not None and created.status_code == 202, (
        created.text if created is not None else "Typed G72 task was not created"
    )
    task_id = created.json()["taskId"]
    if not claim_after_create:
        return task_id, source_id, project["id"], tenant_id, requested_by, 0
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    return task_id, source_id, project["id"], tenant_id, requested_by, claimed.json()["leaseEpoch"]


def _wait_until_postgres_reports_lock_wait(
    sessions, table_name: str = "generation_tasks", timeout_seconds: float = 8.0
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with sessions() as observer:
            waiting = observer.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND pid <> pg_backend_pid()
                      AND wait_event_type = 'Lock'
                      AND query ILIKE :table_pattern
                    """
                ),
                {"table_pattern": f"%{table_name}%"},
            ).scalar_one()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def test_g72_postgres_historical_reserved_owner_transfer_is_quarantined_without_queue_starvation(
    postgres_context,
):
    """A pre-transfer FAKE QUEUED+RESERVED task must not consume a Worker lease.

    Exercise the native API against disposable PostgreSQL. This only tests the
    safe hold-and-skip behavior. It does not scan real historical records,
    authorize quota refunds, modify a protected store, or implement grants.
    """
    client, sessions, app = postgres_context
    legacy_id, _, legacy_project_id, tenant_id, original_owner, _ = (
        _g72_create_claimed_typed_fake_source(
            client, sessions, "unused-legacy", source_sha="a" * 64,
            claim_after_create=False,
        )
    )
    healthy_id, _, _, healthy_tenant_id, healthy_owner, _ = (
        _g72_create_claimed_typed_fake_source(
            client, sessions, "unused-healthy", source_sha="b" * 64,
            claim_after_create=False,
        )
    )
    assert tenant_id == healthy_tenant_id and original_owner == healthy_owner
    replacement_owner = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        legacy = db.get(DBGenerationTask, legacy_id)
        project = db.get(DBProject, legacy_project_id)
        assert legacy is not None and project is not None
        assert legacy.status == "QUEUED" and legacy.lease_owner is None
        assert project.owner_id == original_owner
        db.add(DBUser(
            id=replacement_owner, tenant_id=tenant_id,
            email=f"g72-legacy-transfer-{replacement_owner}@example.com",
            display_name="Synthetic Replacement Owner",
            password_hash=hash_password(secrets.token_urlsafe(24)),
            role="owner", is_active=True, created_at=now, updated_at=now,
        ))
        db.flush()  # PostgreSQL FK requires the replacement user before owner transfer.
        project.owner_id = replacement_owner
        db.commit()
        legacy = db.get(DBGenerationTask, legacy_id)
        assert legacy.requested_by != db.get(DBProject, legacy_project_id).owner_id
        old_lease = (legacy.status, legacy.lease_owner, legacy.lease_epoch)
        old_attempts = db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == legacy_id,
        )).scalar_one()
        old_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == legacy_id,
        )).scalar_one()
        old_usage = [
            (entry.kind, entry.request_units, entry.generated_seconds)
            for entry in db.execute(select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == legacy_id,
            )).scalars()
        ]
        assert old_attempts == 1 and len(old_usage) == 1
        assert old_usage[0][0] == "RESERVED"

    # The older unclaimable reservation must not block a subsequent healthy job.
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": "worker-g72-legacy-readonly",
    }
    claimed = client.post("/internal/generation-tasks/claim", headers=worker_headers)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id
    empty = client.post("/internal/generation-tasks/claim", headers=worker_headers)
    assert empty.status_code == 204, empty.text

    with sessions() as db:
        old = db.get(DBGenerationTask, legacy_id)
        new = db.get(DBGenerationTask, healthy_id)
        assert old is not None and new is not None
        assert (old.status, old.lease_owner, old.lease_epoch) == old_lease
        assert db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == legacy_id,
        )).scalar_one() == old_attempts
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == legacy_id,
        )).scalar_one() == old_events
        current_usage = [
            (entry.kind, entry.request_units, entry.generated_seconds)
            for entry in db.execute(select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == legacy_id,
            )).scalars()
        ]
        assert current_usage == old_usage
        assert new.lease_owner == "worker-g72-legacy-readonly"
    assert app.state.video_use.media_objects == {}
    app.state.moneyprinter.assert_not_called()


def test_g72_postgres_fake_talking_head_create_and_worker_claim(postgres_context):
    """The normal G72 FAKE entry creates its task/attempt and reaches the Worker queue on PostgreSQL."""
    client, sessions, _app = postgres_context
    project_response = client.post("/projects", json={"name": "G72 PostgreSQL FAKE route"})
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    with sessions() as db:
        project_row = db.get(DBProject, project["id"])
        assert project_row is not None
        now = datetime.datetime.now(datetime.timezone.utc)
        asset = DBAssetVersion(
            id=str(uuid.uuid4()),
            tenant_id=project_row.tenant_id,
            project_id=project_row.id,
            media_id="g72-postgres-synthetic-image",
            version_no=1,
            sha256="a" * 64,
            media_type="image",
            content_type="image/png",
            size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project_row.owner_id,
            created_at=now,
        )
        rights = DBRightsSnapshot(
            id=str(uuid.uuid4()),
            tenant_id=project_row.tenant_id,
            project_id=project_row.id,
            asset_version_id=asset.id,
            status="ALLOWED",
            purpose="REMIX",
            territory="TEST",
            valid_from=None,
            valid_until=None,
            evidence_ref="synthetic-postgres-test-evidence",
            captured_by=project_row.owner_id,
            captured_at=now,
        )
        asset_id = asset.id
        db.add_all([asset, rights])
        db.commit()

    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, "g72-postgres-fake-entry"))
    payload = {
        "text": "synthetic offline voice-over",
        "durationSeconds": 1,
        "aspectRatio": "9:16",
        "resolution": "720p",
        "voiceId": "synthetic-seed",
        "imageAssetVersionId": asset_id,
        "idempotencyKey": key,
        "expectedProjectRevision": project["revision"],
        "confirmSyntheticTestOnly": True,
    }

    created = None
    stale_codes = {"CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED"}
    for _ in range(3):
        capabilities = client.get("/generation/providers/moneyprinter/capabilities")
        assert capabilities.status_code == 200, capabilities.text
        payload["capabilitySnapshotHash"] = capabilities.json()["snapshotHash"]
        preflight = client.post(path + "/validate", json=payload)
        if preflight.status_code != 200:
            code = preflight.json().get("detail", {}).get("code")
            if code in stale_codes:
                continue
            pytest.fail(f"G72 PostgreSQL preflight failed: {preflight.text}")
        payload["capabilitySnapshotHash"] = preflight.json()["capabilitySnapshotHash"]
        created = client.post(path, json=payload)
        if created.status_code == 202:
            break
        code = created.json().get("detail", {}).get("code")
        if code not in stale_codes:
            break

    assert created is not None and created.status_code == 202, (
        created.text if created is not None else "G72 create was not attempted"
    )
    task_id = created.json()["taskId"]

    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-g72-postgres"},
    )
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["taskId"] == task_id
    assert body["providerMode"] == "talking-head-fake"
    assert body["request"]["imageAssetId"] == asset_id
    assert body["request"]["fakeTaskType"] == "talking-head"

    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        assert stored is not None
        assert stored.provider == "deterministic-fake-talking-head"
        assert stored.lease_owner == "worker-g72-postgres"
        assert stored.lease_epoch == 1
        attempts = list(
            db.execute(
                select(DBGenerationAttempt).where(
                    DBGenerationAttempt.generation_task_id == task_id
                )
            ).scalars()
        )
        assert len(attempts) == 1





def test_g72_postgres_editor_cannot_cancel_claimed_owner_fake_and_release_quota(
    postgres_context,
):
    """Native disposable PostgreSQL: Editor denial cannot cancel an Owner lease.

    This protects the existing FAKE Worker ownership contract; it does NOT
    approve cross-project grants, refunds or historical orphan reconciliation.
    """
    client, sessions, app = postgres_context
    task_id, _source, project_id, tenant_id, owner_id, _epoch = (
        _g72_create_claimed_typed_fake_source(
            client, sessions, "worker-g72-actor-cancel", source_sha="b" * 64
        )
    )
    editor_id = "g72-pg-cancel-editor-" + str(uuid.uuid4())
    password = secrets.token_urlsafe(24)
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.tenant_id == tenant_id
        assert task.requested_by == owner_id and task.lease_owner is not None
        before_state = (
            task.status, task.lease_owner, task.lease_epoch,
            task.cancel_requested_at, task.completed_at,
        )
        before_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id
        )).scalar_one()
        before_usage = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id
        )).scalar_one()
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        db.add(DBUser(
            id=editor_id, tenant_id=tenant_id,
            email=f"{editor_id}@example.com",
            display_name="Synthetic PG Unassigned Editor",
            password_hash=hash_password(password), role="editor", is_active=True,
            created_at=now, updated_at=now,
        ))
        db.commit()

    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={
        "email": f"{editor_id}@example.com", "password": password,
    }).status_code == 200
    denied = client.post(
        f"/projects/{project_id}/generation-tasks/{task_id}/cancel"
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["code"] == (
        "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER"
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert (
            task.status, task.lease_owner, task.lease_epoch,
            task.cancel_requested_at, task.completed_at,
        ) == before_state
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id
        )).scalar_one() == before_events
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id
        )).scalar_one() == before_usage
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
    assert app.state.video_use.media_objects == {}
    app.state.moneyprinter.assert_not_called()
    # Positive control: original Owner retains the ordinary cancellation path
    # before any upstream acceptance. This does not authorize paid refunds.
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={
        "email": "g76-postgres-owner@example.com", "password": OWNER_PASSWORD,
    }).status_code == 200
    owner_cancel = client.post(
        f"/projects/{project_id}/generation-tasks/{task_id}/cancel"
    )
    assert owner_cancel.status_code == 200, owner_cancel.text
    assert owner_cancel.json()["status"] == "CANCELED"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task.status == "CANCELED"
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()

def test_g72_postgres_unassigned_editor_is_denied_before_reservation(
    postgres_context,
):
    """FAKE-only disposable PostgreSQL: denied Editor cannot create orphan usage.

    This is a fail-closed typed-entry regression aligned to existing Worker
    Owner-only claim. It neither implements grants/delegation nor repairs
    already stranded historical RESERVED entries.
    """
    client, sessions, app = postgres_context
    project_response = client.post(
        "/projects", json={"name": "G72 PG unassigned editor diagnostic"},
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    editor_id = "g72-pg-editor-" + str(uuid.uuid4())
    editor_password = secrets.token_urlsafe(24)
    source_id = str(uuid.uuid4())
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        row = db.get(DBProject, project["id"])
        assert row is not None
        assert row.owner_id != editor_id
        tenant_id = row.tenant_id
        db.add(DBAssetVersion(
            id=source_id, tenant_id=row.tenant_id, project_id=row.id,
            media_id="g72-pg-unassigned-synthetic-" + source_id,
            version_no=1, sha256="a" * 64, media_type="image",
            content_type="image/png", size_bytes=53,
            probe_json={"image": {"width": 16, "height": 16}},
            created_by=row.owner_id, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=row.tenant_id, project_id=row.id,
            asset_version_id=source_id, status="ALLOWED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="g72-pg-unassigned-disposable-only",
            captured_by=row.owner_id, captured_at=now,
        ))
        db.add(DBUser(
            id=editor_id, tenant_id=row.tenant_id,
            email=f"{editor_id}@example.com",
            display_name="Unassigned PostgreSQL Editor",
            password_hash=hash_password(editor_password),
            role="editor", is_active=True,
            created_at=now, updated_at=now,
        ))
        db.commit()

    logout = client.post("/auth/logout")
    assert logout.status_code == 204, logout.text
    login = client.post(
        "/auth/login",
        json={"email": f"{editor_id}@example.com", "password": editor_password},
    )
    assert login.status_code == 200, login.text
    base = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    payload = {
        "text": "synthetic isolated PG actor scope", "durationSeconds": 1,
        "aspectRatio": "9:16", "resolution": "720p",
        "voiceId": "synthetic-seed", "imageAssetVersionId": source_id,
        "idempotencyKey": str(uuid.uuid4()),
        "expectedProjectRevision": project["revision"],
        "confirmSyntheticTestOnly": True,
    }
    capabilities = client.get("/generation/providers/moneyprinter/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    payload["capabilitySnapshotHash"] = capabilities.json()["snapshotHash"]
    for endpoint in (base + "/validate", base, base):
        rejected = client.post(endpoint, json=payload)
        assert rejected.status_code == 403, rejected.text
        assert rejected.json()["detail"]["code"] == (
            "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER"
        )
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id)).where(
            DBGenerationTask.tenant_id == tenant_id,
            DBGenerationTask.idempotency_key == payload["idempotencyKey"],
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id))).scalar_one() == 0
    assert app.state.video_use.media_objects == {}
    app.state.moneyprinter.assert_not_called()
    no_lease = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN,
                 "X-Worker-Id": "worker-g72-pg-unassigned"},
    )
    assert no_lease.status_code == 204, no_lease.text

    # Positive control on the same tenant/project/source: original Owner
    # remains eligible for the existing FAKE Worker claim path.
    assert client.post("/auth/logout").status_code == 204
    owner_login = client.post("/auth/login", json={
        "email": "g76-postgres-owner@example.com", "password": OWNER_PASSWORD,
    })
    assert owner_login.status_code == 200, owner_login.text
    owner_capabilities = client.get("/generation/providers/moneyprinter/capabilities")
    assert owner_capabilities.status_code == 200, owner_capabilities.text
    payload["capabilitySnapshotHash"] = owner_capabilities.json()["snapshotHash"]
    payload["idempotencyKey"] = str(uuid.uuid4())
    owner_preflight = client.post(base + "/validate", json=payload)
    assert owner_preflight.status_code == 200, owner_preflight.text
    payload["capabilitySnapshotHash"] = owner_preflight.json()["capabilitySnapshotHash"]
    created = client.post(base, json=payload)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "QUEUED"
        assert task.tenant_id == tenant_id and task.project_id == project["id"]
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
    eligible_claim = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN,
                 "X-Worker-Id": "worker-g72-pg-owner"},
    )
    assert eligible_claim.status_code == 200, eligible_claim.text
    assert eligible_claim.json()["taskId"] == task_id
    assert app.state.video_use.media_objects == {}
    app.state.moneyprinter.assert_not_called()



def test_g76_postgres_artifact_success_waits_for_circuit_lock_and_preserves_reopen(postgres_context):
    """Artifact success must re-read a circuit changed by another PostgreSQL transaction."""
    owner_client, sessions, app = postgres_context
    task_id = _create_task(owner_client)

    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id
        db.add(
            DBGenerationCircuitState(
                id=str(uuid.uuid4()),
                tenant_id=task.tenant_id,
                provider="moneyprinter",
                state="HALF_OPEN",
                failure_timestamps_json=[now.isoformat()],
                opened_at=now - datetime.timedelta(minutes=2),
                cooldown_until=now - datetime.timedelta(seconds=1),
                half_open_task_id=None,
                probe_epoch=0,
                half_open_lease_epoch=None,
                half_open_issuer=None,
                updated_at=now,
            )
        )
        db.commit()

    worker_id = "worker-g76-circuit-lock"
    claimed = owner_client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    lease_epoch = claimed.json()["leaseEpoch"]
    probe_epoch = claimed.json()["circuitProbeEpoch"]
    assert probe_epoch is not None

    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(lease_epoch),
    }
    running = owner_client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running",
              "upstreamJobId": "g76-pg-upstream"},
    )
    assert running.status_code == 200, running.text
    ingesting = owner_client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "INGESTING", "progress": 95, "message": "synthetic ingest",
              "upstreamJobId": "g76-pg-upstream", "providerArtifactId": "g76-pg-artifact"},
    )
    assert ingesting.status_code == 200, ingesting.text

    locker = sessions()
    try:
        circuit = locker.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            ).with_for_update()
        ).scalar_one()

        def ingest_artifact():
            with TestClient(app) as worker_client:
                return worker_client.post(
                    f"/internal/generation-tasks/{task_id}/artifact-intake",
                    headers=worker_headers,
                    data={"providerArtifactId": "g76-pg-artifact"},
                    files={"file": ("g76-postgres-fake.mp4", b"synthetic-fake-video", "video/mp4")},
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ingest_artifact)
            try:
                assert _wait_until_postgres_reports_lock_wait(
                    sessions, table_name="generation_circuit_states"
                ), "artifact intake did not wait on the PostgreSQL circuit-state row lock"

                circuit.state = "OPEN"
                circuit.opened_at = datetime.datetime.now(datetime.timezone.utc)
                circuit.cooldown_until = (
                    datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2)
                )
                circuit.half_open_task_id = None
                circuit.half_open_lease_epoch = None
                circuit.half_open_issuer = None
                circuit.probe_epoch += 1
                circuit.updated_at = datetime.datetime.now(datetime.timezone.utc)
                locker.commit()
            finally:
                if locker.in_transaction():
                    locker.rollback()

            response = future.result(timeout=10)

        assert response.status_code == 201, response.text
        with sessions() as verify:
            final_circuit = verify.execute(
                select(DBGenerationCircuitState).where(
                    DBGenerationCircuitState.tenant_id == tenant_id,
                    DBGenerationCircuitState.provider == "moneyprinter",
                )
            ).scalar_one()
            closed_events = verify.execute(
                select(DBGenerationProviderEvent.id).where(
                    DBGenerationProviderEvent.tenant_id == tenant_id,
                    DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED",
                )
            ).all()
            assert final_circuit.state == "OPEN"
            assert final_circuit.half_open_task_id is None
            assert final_circuit.half_open_lease_epoch is None
            assert final_circuit.half_open_issuer is None
            assert len(closed_events) == 0
    finally:
        locker.rollback()
        locker.close()


def test_g76_postgres_heartbeat_waits_for_task_lock_and_rechecks_new_lease(postgres_context):
    """A heartbeat cannot validate an old lease before a concurrent re-claim commits."""
    owner_client, sessions, app = postgres_context
    task_id = _create_task(owner_client)

    claimed = owner_client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-old"},
    )
    assert claimed.status_code == 200, claimed.text
    old_epoch = claimed.json()["leaseEpoch"]
    assert claimed.json()["taskId"] == task_id

    locker = sessions()
    try:
        task = locker.execute(
            select(DBGenerationTask)
            .where(DBGenerationTask.id == task_id)
            .with_for_update()
        ).scalar_one()

        def send_old_heartbeat():
            with TestClient(app) as worker_client:
                return worker_client.post(
                    f"/internal/generation-tasks/{task_id}/heartbeat",
                    headers={
                        "X-Worker-Token": WORKER_TOKEN,
                        "X-Worker-Id": "worker-old",
                        "X-Worker-Lease-Epoch": str(old_epoch),
                    },
                    json={},
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(send_old_heartbeat)
            try:
                assert _wait_until_postgres_reports_lock_wait(sessions), (
                    "heartbeat did not wait on the PostgreSQL generation_tasks row lock"
                )

                task.lease_owner = "worker-new"
                task.lease_epoch = old_epoch + 1
                task.lease_expires_at = (
                    datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(minutes=5)
                )
                task.updated_at = datetime.datetime.now(datetime.timezone.utc)
                locker.commit()
            finally:
                # Never leave the worker thread blocked if the lock assertion fails.
                if locker.in_transaction():
                    locker.rollback()

            response = future.result(timeout=10)

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "LEASE_LOST"

        with sessions() as verify:
            current = verify.get(DBGenerationTask, task_id)
            assert current.lease_owner == "worker-new"
            assert current.lease_epoch == old_epoch + 1
            assert current.lease_expires_at is not None
    finally:
        locker.rollback()
        locker.close()


def test_g76_postgres_additive_migration_is_idempotent_and_preserves_legacy_rows(
    postgres_context,
):
    """The draft PostgreSQL migration must safely upgrade a legacy schema twice."""
    _client, sessions, _app = postgres_context
    with sessions() as db:
        engine = db.get_bind()

    schema = f"g76_migration_{uuid.uuid4().hex[:12]}"
    migration_path = Path(__file__).parent / "migrations" / "20261030_g76_probe_epoch_issuer.sql"
    migration_sql = "\n".join(
        line
        for line in migration_path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    statements = [
        statement.strip()
        for statement in migration_sql.split(";")
        if statement.strip()
    ]
    assert len(statements) == 5

    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection.execute(text("CREATE TABLE generation_tasks (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE generation_circuit_states (id VARCHAR PRIMARY KEY)"))
        connection.execute(text("INSERT INTO generation_tasks (id) VALUES ('legacy-task')"))
        connection.execute(
            text("INSERT INTO generation_circuit_states (id) VALUES ('legacy-circuit')")
        )

        # Running the same additive migration twice proves IF NOT EXISTS behavior.
        for _ in range(2):
            for statement in statements:
                connection.execute(text(statement))

        columns = connection.execute(
            text(
                """
                SELECT table_name, column_name, is_nullable, column_default, data_type
                FROM information_schema.columns
                WHERE table_schema = :schema
                """
            ),
            {"schema": schema},
        ).all()
        actual = {
            (row.table_name, row.column_name): (
                row.is_nullable,
                row.column_default,
                row.data_type,
            )
            for row in columns
        }

        expected = {
            ("generation_tasks", "lease_epoch"),
            ("generation_tasks", "circuit_probe_epoch"),
            ("generation_circuit_states", "probe_epoch"),
            ("generation_circuit_states", "half_open_lease_epoch"),
            ("generation_circuit_states", "half_open_issuer"),
        }
        assert expected.issubset(actual)

        for key in (
            ("generation_tasks", "lease_epoch"),
            ("generation_circuit_states", "probe_epoch"),
        ):
            nullable, default, data_type = actual[key]
            assert nullable == "NO"
            assert default is not None and "0" in default
            assert data_type == "integer"

        assert actual[("generation_tasks", "circuit_probe_epoch")][0] == "YES"
        assert actual[("generation_circuit_states", "half_open_lease_epoch")][0] == "YES"
        assert actual[("generation_circuit_states", "half_open_issuer")][0] == "YES"
        assert actual[("generation_circuit_states", "half_open_issuer")][2] == "character varying"

        task_row = connection.execute(
            text(
                "SELECT id, lease_epoch, circuit_probe_epoch "
                "FROM generation_tasks WHERE id = 'legacy-task'"
            )
        ).one()
        circuit_row = connection.execute(
            text(
                "SELECT id, probe_epoch, half_open_lease_epoch, half_open_issuer "
                "FROM generation_circuit_states WHERE id = 'legacy-circuit'"
            )
        ).one()
        assert task_row.lease_epoch == 0
        assert task_row.circuit_probe_epoch is None
        assert circuit_row.probe_epoch == 0
        assert circuit_row.half_open_lease_epoch is None
        assert circuit_row.half_open_issuer is None

        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

@pytest.mark.parametrize("corrupt_quota_counter", [False, True])
def test_g72_postgres_artifact_upload_failure_compensates_storage_case(
    postgres_context, monkeypatch, corrupt_quota_counter,
):
    """Native PostgreSQL: damaged quota counters must not certify a refund."""
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        project_id = task.project_id
        tenant_id = task.tenant_id

    worker_id = "worker-g72-pg-upload-failure"
    upstream_id = "g72-pg-upload-failure-upstream"
    artifact_id = "g72-pg-upload-failure-artifact"
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    epoch = claimed.json()["leaseEpoch"]
    assert claimed.json()["taskId"] == task_id
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(epoch),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running", "upstreamJobId": upstream_id},
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95, "message": "synthetic ingesting",
            "upstreamJobId": upstream_id, "providerArtifactId": artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    failure_payload = b"g72-postgres-failure-bytes"

    def fail_upload(*_args, **_kwargs):
        if corrupt_quota_counter:
            # The API already durably reserved the payload. Simulate
            # independent ledger corruption before its compensation path.
            with sessions() as independent:
                tenant = independent.get(DBTenant, tenant_id)
                assert tenant is not None
                assert tenant.used_storage_bytes == len(failure_payload)
                tenant.used_storage_bytes = 0
                independent.commit()
        raise VideoUseError("synthetic PostgreSQL sidecar upload failure")

    monkeypatch.setattr(app.state.video_use, "upload_media", fail_upload)
    response = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=worker_headers,
        data={"providerArtifactId": artifact_id},
        files={"file": ("synthetic.mp4", failure_payload, "video/mp4")},
    )
    assert response.status_code == (409 if corrupt_quota_counter else 502), response.text
    assert response.json()["detail"]["code"] == (
        "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
        if corrupt_quota_counter else "ARTIFACT_STORE_FAILED"
    )

    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        assert stored.status == ("INGESTING" if corrupt_quota_counter else "FAILED")
        if not corrupt_quota_counter:
            assert stored.error_code == "ARTIFACT_INGEST_FAILED"
        assert tenant.used_storage_bytes == 0
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_type == "video",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one() == (0 if corrupt_quota_counter else 1)
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        if not corrupt_quota_counter:
            assert attempt.reconciliation_state == "HELD"
        else:
            # No release receipt or failure event may certify an underfunded refund.
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
            )).scalar_one() == 1
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
            )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()


def _prepare_g70_g71_postgres_duplicate_intake(client):
    task_id = _create_task(client)
    worker_id = "worker-g70-g71-duplicate-intake"
    upstream_id = f"g70-g71-upstream-{task_id}"
    artifact_id = f"g70-g71-artifact-{task_id}"
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(claimed.json()["leaseEpoch"]),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "RUNNING",
            "progress": 10,
            "message": "synthetic G70/G71 running",
            "upstreamJobId": upstream_id,
        },
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING",
            "progress": 95,
            "message": "synthetic G70/G71 ingesting",
            "upstreamJobId": upstream_id,
            "providerArtifactId": artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text
    return task_id, worker_headers, artifact_id


def _assert_g70_g71_postgres_single_ingest(sessions, task_id, payload):
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant = db.get(DBTenant, task.tenant_id)
        assert tenant is not None
        attempts = list(db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalars())
        assets = list(db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.project_id == task.project_id,
                DBAssetVersion.media_id == task.media_id,
            )
        ).scalars())
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(DBGenerationUsageEntry.task_id == task_id)
        ).scalars())
        events = list(db.execute(
            select(DBGenerationEvent).where(DBGenerationEvent.generation_task_id == task_id)
        ).scalars())

        assert task.status == "RIGHTS_BLOCKED"
        assert task.attempts == 1
        assert task.asset_version_id is not None
        assert task.media_id == _expected_media_id(
            task.project_id, f"generation:{task_id}"
        )
        assert len(attempts) == 1
        assert attempts[0].reconciliation_state == "ARTIFACT_INGESTED"
        assert len(assets) == 1
        assert assets[0].id == task.asset_version_id
        assert assets[0].sha256 == hashlib.sha256(payload).hexdigest()
        assert tenant.used_storage_bytes == len(payload)
        assert sum(row.kind == "RESERVED" for row in usage) == 1
        assert sum(row.kind == "SETTLED" for row in usage) == 1
        assert sum(row.kind == "RELEASED" for row in usage) == 0
        assert sum(row.event_type == "ARTIFACT_INGESTED" for row in events) == 1
        assert sum(row.event_type == "ARTIFACT_INGEST_FAILED" for row in events) == 0
        release_events = [
            row for row in events if row.event_type == "STORAGE_RESERVATION_RELEASED"
        ]
        assert len(release_events) == 1
        assert release_events[0].metadata_json["reservedStorageBytes"] == len(payload)


def _post_g70_g71_duplicate_intake(app, task_id, worker_headers, artifact_id, payload):
    with TestClient(app) as worker_client:
        return worker_client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers=worker_headers,
            data={"providerArtifactId": artifact_id},
            files={"file": ("synthetic-g70-g71.mp4", payload, "video/mp4")},
        )


@pytest.mark.parametrize(
    "corruption", [
        "noninteger_release_bytes", "wrong_release_digest", "wrong_actor", "invalid_reason",
        "paired_missing_worker_id", "paired_missing_provider_artifact",
        "paired_missing_media_id", "paired_missing_sha256",
        "paired_container_sha256", "forged_opened_actor",
        "container_release_reservation_id_list", "container_release_reservation_id_object",
        "container_release_reason_list", "container_release_reason_object",
        "duplicate_valid_release", "valid_and_malformed_duplicate",
        "valid_and_unknown_reference", "valid_and_untyped_reference",
        "valid_and_unstructured_receipt",
        "paired_missing_liveness_key", "paired_boolean_liveness_key",
        "paired_wrong_liveness_key", "paired_unparseable_opened_id",
        "paired_opened_attempt_only", "paired_release_attempt_only",
        "paired_foreign_task_attempt", "paired_release_predates_opened",
    ]
)
def test_g70_g71_postgres_corrupt_release_receipt_does_not_clear_open_hold(
    postgres_context, monkeypatch, corruption,
):
    """A release audit row is not quota proof unless it matches its opened hold.

    Disposable PostgreSQL and FAKE-only Sidecar. A malformed release receipt
    must not suppress orphan protection, reserve again, or invoke the Sidecar.
    """
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-corrupt-release-receipt-must-not-duplicate-quota"
    opened_id = (
        "legacy-not-a-uuid-" + str(uuid.uuid4())
        if corruption == "paired_unparseable_opened_id"
        else str(uuid.uuid4())
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    foreign_task_id = (
        _create_task(owner_client)
        if corruption == "paired_foreign_task_attempt" else None
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        tenant = db.get(DBTenant, task.tenant_id)
        assert tenant is not None
        initial_used = tenant.used_storage_bytes
        tenant.used_storage_bytes += len(payload)
        own_attempt_id = db.execute(
            select(DBGenerationAttempt.id).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        opened_attempt_id = (
            own_attempt_id if corruption == "paired_opened_attempt_only" else None
        )
        release_attempt_id = (
            own_attempt_id if corruption == "paired_release_attempt_only" else None
        )
        if foreign_task_id is not None:
            # _create_task already inserts attempt 1; reuse the real,
            # foreign-task FK instead of creating a duplicate attempt.
            foreign_attempt_id = db.execute(
                select(DBGenerationAttempt.id).where(
                    DBGenerationAttempt.generation_task_id == foreign_task_id
                )
            ).scalar_one()
            opened_attempt_id = release_attempt_id = foreign_attempt_id
        opened_metadata = {
            "workerId": worker_headers["X-Worker-Id"],
            "leaseEpoch": int(worker_headers["X-Worker-Lease-Epoch"]),
            "providerArtifactId": artifact_id,
            "mediaId": _expected_media_id(task.project_id, f"generation:{task_id}"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "reservedStorageBytes": len(payload),
            # Use a valid, event-ID-derived lock identity by default: otherwise
            # all corrupt-release cases pass for an unrelated missing-key
            # reason and never test their intended rejection condition.
            "livenessLockKey": (
                int(uuid.UUID(opened_id)) & 0x7FFFFFFFFFFFFFFF
                if corruption != "paired_unparseable_opened_id" else 0
            ),
        }
        release_metadata = {
            **opened_metadata,
            "storageReservationEventId": opened_id,
            "reasonCode": "DUPLICATE_ARTIFACT_INTAKE",
        }
        actor = "SYSTEM"
        opened_actor = "SYSTEM"
        if corruption == "paired_missing_liveness_key":
            opened_metadata.pop("livenessLockKey")
        elif corruption == "paired_boolean_liveness_key":
            # bool is an int subclass in Python; accept only a real PG key.
            opened_metadata["livenessLockKey"] = True
        elif corruption == "paired_wrong_liveness_key":
            # Wrong but well-typed lock keys must not authenticate a release.
            opened_metadata["livenessLockKey"] = (
                (opened_metadata["livenessLockKey"] + 1) & 0x7FFFFFFFFFFFFFFF
            )
        elif corruption == "noninteger_release_bytes":
            release_metadata["reservedStorageBytes"] = str(len(payload))
        elif corruption == "wrong_release_digest":
            release_metadata["sha256"] = "0" * 64
        elif corruption == "wrong_actor":
            actor = "OWNER"
        elif corruption == "invalid_reason":
            release_metadata["reasonCode"] = "UNTRUSTED_RELEASE"
        elif corruption in {
            "paired_missing_worker_id", "paired_missing_provider_artifact",
            "paired_missing_media_id", "paired_missing_sha256",
        }:
            key = {
                "paired_missing_worker_id": "workerId",
                "paired_missing_provider_artifact": "providerArtifactId",
                "paired_missing_media_id": "mediaId",
                "paired_missing_sha256": "sha256",
            }[corruption]
            # Same absent field in both events used to satisfy None == None.
            opened_metadata.pop(key)
            release_metadata.pop(key)
        elif corruption == "paired_container_sha256":
            opened_metadata["sha256"] = ["forged", "not-a-hash"]
            release_metadata["sha256"] = ["forged", "not-a-hash"]
        elif corruption == "forged_opened_actor":
            opened_actor = "OWNER"
        elif corruption == "container_release_reservation_id_list":
            release_metadata["storageReservationEventId"] = ["unhashable", opened_id]
        elif corruption == "container_release_reservation_id_object":
            release_metadata["storageReservationEventId"] = {"id": opened_id}
        elif corruption == "container_release_reason_list":
            release_metadata["reasonCode"] = ["DUPLICATE_ARTIFACT_INTAKE"]
        elif corruption == "container_release_reason_object":
            release_metadata["reasonCode"] = {"reason": "UPLOAD_INTERRUPTED"}
        db.add(DBGenerationEvent(
            id=opened_id, generation_task_id=task_id, attempt_id=opened_attempt_id,
            event_type="STORAGE_RESERVATION_OPENED", from_status="INGESTING",
            to_status="INGESTING", actor_type=opened_actor, actor_id="api",
            metadata_json=opened_metadata, created_at=now,
        ))
        db.add(DBGenerationEvent(
            id=str(uuid.uuid4()), generation_task_id=task_id, attempt_id=release_attempt_id,
            event_type="STORAGE_RESERVATION_RELEASED", from_status="INGESTING",
            to_status="INGESTING", actor_type=actor, actor_id="api",
            metadata_json=release_metadata, created_at=(
                now - datetime.timedelta(seconds=60)
                if corruption == "paired_release_predates_opened" else now
            ),
        ))
        if corruption in {
            "duplicate_valid_release", "valid_and_malformed_duplicate",
            "valid_and_unknown_reference", "valid_and_untyped_reference",
            "valid_and_unstructured_receipt",
        }:
            # A single legitimate receipt must not mask a second competing
            # or unmatchable audit row. No new quota hold or Sidecar call.
            extra_metadata = dict(release_metadata)
            if corruption == "valid_and_malformed_duplicate":
                extra_metadata["sha256"] = "0" * 64
            elif corruption == "valid_and_unknown_reference":
                extra_metadata["storageReservationEventId"] = str(uuid.uuid4())
            elif corruption == "valid_and_untyped_reference":
                extra_metadata["storageReservationEventId"] = ["untrusted", opened_id]
            elif corruption == "valid_and_unstructured_receipt":
                extra_metadata = ["untrusted", opened_id]
            db.add(DBGenerationEvent(
                id=str(uuid.uuid4()), generation_task_id=task_id, attempt_id=None,
                event_type="STORAGE_RESERVATION_RELEASED", from_status="INGESTING",
                to_status="INGESTING", actor_type="SYSTEM", actor_id="api",
                metadata_json=extra_metadata, created_at=now,
            ))
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    replay = _post_g70_g71_duplicate_intake(
        app, task_id, worker_headers, artifact_id, payload
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    sidecar_upload.assert_not_called()
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, stored.tenant_id)
        assert stored.status == "INGESTING"
        assert stored.asset_version_id is None
        assert tenant.used_storage_bytes == initial_used + len(payload)
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == stored.project_id
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
        )).scalar_one() == 1
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("receipt_shape", ["structured_orphan", "unstructured_orphan"])
def test_g70_g71_postgres_release_receipt_without_any_opened_hold_blocks_intake(
    postgres_context, monkeypatch, receipt_shape,
):
    """No OPENED event + a dangling RELEASED is ambiguous durable audit.

    Disposable native PostgreSQL and FAKE Sidecar, without any actual
    quota mutation, Provider call or historical reconciliation.
    """
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-release-without-opened-fake-only"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        tenant_id, project_id = task.tenant_id, task.project_id
        initial_used = db.get(DBTenant, tenant_id).used_storage_bytes
        release_metadata = {
            "storageReservationEventId": str(uuid.uuid4()),
            "reasonCode": "UPLOAD_INTERRUPTED",
            "workerId": worker_headers["X-Worker-Id"],
            "leaseEpoch": int(worker_headers["X-Worker-Lease-Epoch"]),
            "providerArtifactId": artifact_id,
            "mediaId": _expected_media_id(project_id, f"generation:{task_id}"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "reservedStorageBytes": len(payload),
        }
        if receipt_shape == "unstructured_orphan":
            release_metadata = ["legacy", "dangling-release"]
        db.add(DBGenerationEvent(
            id=str(uuid.uuid4()), generation_task_id=task_id, attempt_id=None,
            event_type="STORAGE_RESERVATION_RELEASED",
            from_status="INGESTING", to_status="INGESTING",
            actor_type="SYSTEM", actor_id="api",
            metadata_json=release_metadata,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        ))
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    replay = _post_g70_g71_duplicate_intake(
        app, task_id, worker_headers, artifact_id, payload
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    sidecar_upload.assert_not_called()
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        assert task.asset_version_id is None
        assert db.get(DBTenant, tenant_id).used_storage_bytes == initial_used
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == 0
        for event_type, expected_count in [
            ("STORAGE_RESERVATION_OPENED", 0),
            ("STORAGE_RESERVATION_RELEASED", 1),
            ("ARTIFACT_INGESTED", 0),
        ]:
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == event_type,
            )).scalar_one() == expected_count
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize(
    "corruption", ["wrong_live_lock_key", "unparseable_opened_event_id"],
)
def test_g70_g71_postgres_unreleased_opened_lock_must_bind_event_id(
    postgres_context, monkeypatch, corruption,
):
    """An unrelated live PostgreSQL lock cannot validate an OPENED quota hold.

    All other task/lease/digest/issuer identities are valid. The old code
    checked only that the unrelated lock was live, allowing a second debit.
    Disposable FAKE-only PostgreSQL; no trusted recovery ACK or refund.
    """
    client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        client
    )
    payload = b"g70-g71-unreleased-hold-needs-event-bound-live-lock"
    opened_id = (
        "legacy-not-uuid-" + str(uuid.uuid4())
        if corruption == "unparseable_opened_event_id"
        else str(uuid.uuid4())
    )
    # The lock exists, but cannot be the derived lock for this OPENED event.
    lock_key = (
        int(uuid.uuid4()) & 0x7FFFFFFFFFFFFFFF
        if corruption == "unparseable_opened_event_id"
        else (int(uuid.UUID(opened_id)) + 1) & 0x7FFFFFFFFFFFFFFF
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        tenant_id, project_id = task.tenant_id, task.project_id
        tenant = db.get(DBTenant, tenant_id)
        initial_bytes = tenant.used_storage_bytes
        tenant.used_storage_bytes += len(payload)
        db.add(DBGenerationEvent(
            id=opened_id, generation_task_id=task_id, attempt_id=None,
            event_type="STORAGE_RESERVATION_OPENED",
            from_status="INGESTING", to_status="INGESTING",
            actor_type="SYSTEM", actor_id="api",
            metadata_json={
                "workerId": headers["X-Worker-Id"],
                "leaseEpoch": int(headers["X-Worker-Lease-Epoch"]),
                "livenessLockKey": lock_key,
                "providerArtifactId": artifact_id,
                "mediaId": _expected_media_id(project_id, f"generation:{task_id}"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "reservedStorageBytes": len(payload),
            },
            created_at=datetime.datetime.now(datetime.timezone.utc),
        ))
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    engine = sessions.kw["bind"]
    with engine.connect() as blocker:
        assert blocker.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
        ).scalar_one() is True
        blocker.commit()
        try:
            response = _post_g70_g71_duplicate_intake(
                app, task_id, headers, artifact_id, payload
            )
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == (
                "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
            )
            sidecar_upload.assert_not_called()
            with sessions() as db:
                task = db.get(DBGenerationTask, task_id)
                assert task is not None and task.status == "INGESTING"
                assert task.asset_version_id is None
                assert db.get(DBTenant, tenant_id).used_storage_bytes == (
                    initial_bytes + len(payload)
                )
                assert db.execute(select(func.count(DBAssetVersion.id)).where(
                    DBAssetVersion.project_id == project_id,
                )).scalar_one() == 0
                for event_type, count in (
                    ("STORAGE_RESERVATION_OPENED", 1),
                    ("STORAGE_RESERVATION_RELEASED", 0),
                ):
                    assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                        DBGenerationEvent.generation_task_id == task_id,
                        DBGenerationEvent.event_type == event_type,
                    )).scalar_one() == count
            app.state.moneyprinter.assert_not_called()
        finally:
            assert blocker.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            ).scalar_one() is True
            blocker.commit()


@pytest.mark.parametrize(
    "opened_actor_type,opened_actor_id",
    [("OWNER", "api"), ("SYSTEM", "owner")],
)
def test_g70_g71_postgres_untrusted_opened_issuer_with_live_hold_rejects_replay(
    postgres_context, monkeypatch, opened_actor_type, opened_actor_id,
):
    """A live PG lock cannot authenticate a forged/migrated OPENED audit issuer.

    FAKE-only disposable PostgreSQL; a valid-looking unresolved reservation
    with a live advisory lock must not allow another debit or Sidecar call.
    """
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-untrusted-opened-issuer-must-not-debit-again"
    lock_key = int(uuid.uuid4()) & 0x7FFFFFFFFFFFFFFF
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        tenant_id, project_id = task.tenant_id, task.project_id
        tenant = db.get(DBTenant, tenant_id)
        initial_used = tenant.used_storage_bytes
        tenant.used_storage_bytes += len(payload)
        db.add(DBGenerationEvent(
            id=str(uuid.uuid4()), generation_task_id=task_id, attempt_id=None,
            event_type="STORAGE_RESERVATION_OPENED",
            from_status="INGESTING", to_status="INGESTING",
            actor_type=opened_actor_type, actor_id=opened_actor_id,
            metadata_json={
                "workerId": worker_headers["X-Worker-Id"],
                "leaseEpoch": int(worker_headers["X-Worker-Lease-Epoch"]),
                "livenessLockKey": lock_key,
                "providerArtifactId": artifact_id,
                "mediaId": _expected_media_id(project_id, f"generation:{task_id}"),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "reservedStorageBytes": len(payload),
            },
            created_at=now,
        ))
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    engine = sessions.kw["bind"]
    with engine.connect() as blocker:
        assert blocker.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
        ).scalar_one() is True
        blocker.commit()
        try:
            replay = _post_g70_g71_duplicate_intake(
                app, task_id, worker_headers, artifact_id, payload
            )
            assert replay.status_code == 409, replay.text
            assert replay.json()["detail"]["code"] == (
                "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
            )
            sidecar_upload.assert_not_called()
            with sessions() as db:
                task = db.get(DBGenerationTask, task_id)
                assert task is not None and task.status == "INGESTING"
                assert task.asset_version_id is None
                assert db.get(DBTenant, tenant_id).used_storage_bytes == (
                    initial_used + len(payload)
                )
                assert db.execute(select(func.count(DBAssetVersion.id)).where(
                    DBAssetVersion.project_id == project_id,
                )).scalar_one() == 0
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
                )).scalar_one() == 1
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
                )).scalar_one() == 0
            app.state.moneyprinter.assert_not_called()
        finally:
            assert blocker.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            ).scalar_one() is True
            blocker.commit()


@pytest.mark.parametrize("corruption", ["wrong_release_digest", "invalid_release_reason"])
def test_g70_g71_postgres_invalid_release_with_live_hold_blocks_second_reservation(
    postgres_context, monkeypatch, corruption,
):
    """A live PostgreSQL hold cannot legitimize a corrupt matching RELEASED.

    A corrupt release with the correct reservation ID must fail closed even
    when a separate backend holds its liveness advisory lock. FAKE-only and
    disposable PostgreSQL; no real Provider or protected quota changes.
    """
    owner_client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-live-hold-invalid-release-must-not-duplicate"
    reservation_id = str(uuid.uuid4())
    lock_key = int(uuid.uuid4()) & 0x7FFFFFFFFFFFFFFF
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "INGESTING"
        tenant_id, project_id = task.tenant_id, task.project_id
        tenant = db.get(DBTenant, tenant_id)
        initial_used = tenant.used_storage_bytes
        tenant.used_storage_bytes += len(payload)
        opened_metadata = {
            "workerId": headers["X-Worker-Id"],
            "leaseEpoch": int(headers["X-Worker-Lease-Epoch"]),
            "livenessLockKey": lock_key,
            "providerArtifactId": artifact_id,
            "mediaId": _expected_media_id(project_id, f"generation:{task_id}"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "reservedStorageBytes": len(payload),
        }
        released_metadata = {
            **opened_metadata,
            "storageReservationEventId": reservation_id,
            "reasonCode": "DUPLICATE_ARTIFACT_INTAKE",
        }
        if corruption == "wrong_release_digest":
            released_metadata["sha256"] = "0" * 64
        else:
            released_metadata["reasonCode"] = "UNTRUSTED_RELEASE"
        for event_type, metadata, event_id in [
            ("STORAGE_RESERVATION_OPENED", opened_metadata, reservation_id),
            ("STORAGE_RESERVATION_RELEASED", released_metadata, str(uuid.uuid4())),
        ]:
            db.add(DBGenerationEvent(
                id=event_id, generation_task_id=task_id, attempt_id=None,
                event_type=event_type, from_status="INGESTING",
                to_status="INGESTING", actor_type="SYSTEM", actor_id="api",
                metadata_json=metadata, created_at=now,
            ))
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    engine = sessions.kw["bind"]
    with engine.connect() as blocker:
        acquired = blocker.execute(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": lock_key},
        ).scalar_one()
        assert acquired is True
        blocker.commit()
        try:
            replay = _post_g70_g71_duplicate_intake(
                app, task_id, headers, artifact_id, payload
            )
            assert replay.status_code == 409, replay.text
            assert replay.json()["detail"]["code"] == (
                "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
            )
            sidecar_upload.assert_not_called()
            with sessions() as db:
                task = db.get(DBGenerationTask, task_id)
                assert task is not None and task.asset_version_id is None
                assert task.status == "INGESTING"
                assert db.get(DBTenant, tenant_id).used_storage_bytes == (
                    initial_used + len(payload)
                )
                assert db.execute(select(func.count(DBAssetVersion.id)).where(
                    DBAssetVersion.project_id == project_id,
                )).scalar_one() == 0
                for event_type in (
                    "STORAGE_RESERVATION_OPENED", "STORAGE_RESERVATION_RELEASED"
                ):
                    assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                        DBGenerationEvent.generation_task_id == task_id,
                        DBGenerationEvent.event_type == event_type,
                    )).scalar_one() == 1
            app.state.moneyprinter.assert_not_called()
        finally:
            unlocked = blocker.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": lock_key},
            ).scalar_one()
            blocker.commit()
            assert unlocked is True


def test_g70_g71_postgres_concurrent_duplicate_artifact_intake_is_idempotent(
    postgres_context, monkeypatch
):
    """Two independent HTTP clients must produce one asset/settlement and no false failure."""
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = b"synthetic-g70-g71-concurrent-artifact"
    original_upload = app.state.video_use.upload_media
    upload_barrier = Barrier(2)
    counter_lock = Lock()
    upload_calls = 0

    def synchronized_upload(*args, **kwargs):
        nonlocal upload_calls
        with counter_lock:
            upload_calls += 1
        upload_barrier.wait(timeout=15)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(app.state.video_use, "upload_media", synchronized_upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _post_g70_g71_duplicate_intake,
                app, task_id, worker_headers, artifact_id, payload,
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=30) for future in futures]

    assert upload_calls == 2
    assert [response.status_code for response in responses] == [201, 201], [
        response.text for response in responses
    ]
    results = [response.json()["results"][0] for response in responses]
    assert results[0]["assetVersionId"] == results[1]["assetVersionId"]
    assert results[0]["mediaId"] == results[1]["mediaId"]
    _assert_g70_g71_postgres_single_ingest(sessions, task_id, payload)
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_duplicate_intake_lost_ack_releases_extra_storage_reservation(
    postgres_context, monkeypatch
):
    """A duplicate sidecar ACK lost after first commit must not leak storage reservation."""
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = b"synthetic-g70-g71-lost-duplicate-ack"
    original_upload = app.state.video_use.upload_media
    second_upload_entered = Event()
    first_response_completed = Event()
    counter_lock = Lock()
    upload_calls = 0

    def lose_second_ack_after_first_commit(*args, **kwargs):
        nonlocal upload_calls
        with counter_lock:
            upload_calls += 1
            call_number = upload_calls
        if call_number == 1:
            assert second_upload_entered.wait(15), "second duplicate did not reach sidecar upload"
            return original_upload(*args, **kwargs)
        second_upload_entered.set()
        assert first_response_completed.wait(20), "first intake did not commit before simulated ACK loss"
        raise VideoUseError("synthetic duplicate sidecar acknowledgement lost after first commit")

    monkeypatch.setattr(app.state.video_use, "upload_media", lose_second_ack_after_first_commit)

    def send_and_signal_first_response():
        response = _post_g70_g71_duplicate_intake(
            app, task_id, worker_headers, artifact_id, payload
        )
        first_response_completed.set()
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(send_and_signal_first_response),
            pool.submit(send_and_signal_first_response),
        ]
        responses = [future.result(timeout=30) for future in futures]

    assert upload_calls == 2
    assert [response.status_code for response in responses] == [201, 201], [
        response.text for response in responses
    ]
    results = [response.json()["results"][0] for response in responses]
    assert results[0]["assetVersionId"] == results[1]["assetVersionId"]
    assert results[0]["mediaId"] == results[1]["mediaId"]
    _assert_g70_g71_postgres_single_ingest(sessions, task_id, payload)
    app.state.moneyprinter.assert_not_called()


def _run_g70_g71_duplicate_release_commit_fault(postgres_context, monkeypatch, fault_mode):
    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = f"synthetic-g70-g71-db-commit-{fault_mode}".encode()
    original_upload = app.state.video_use.upload_media
    upload_barrier = Barrier(2)
    upload_lock = Lock()
    upload_calls = 0

    def synchronized_upload(*args, **kwargs):
        nonlocal upload_calls
        with upload_lock:
            upload_calls += 1
        upload_barrier.wait(timeout=15)
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(app.state.video_use, "upload_media", synchronized_upload)

    original_commit = Session.commit
    fault_lock = Lock()
    fault_state = {"injected": False}

    def inject_release_commit_fault(session):
        should_inject = False
        with fault_lock:
            if (
                not fault_state["injected"]
                and any(
                    isinstance(item, DBGenerationEvent)
                    and item.event_type == "STORAGE_RESERVATION_RELEASED"
                    for item in session.new
                )
            ):
                fault_state["injected"] = True
                should_inject = True
        if should_inject:
            if fault_mode == "after_commit":
                original_commit(session)
                raise RuntimeError("synthetic DB commit ACK lost after durable commit")
            raise RuntimeError("synthetic DB commit failed before commit")
        return original_commit(session)

    monkeypatch.setattr(Session, "commit", inject_release_commit_fault)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _post_g70_g71_duplicate_intake,
                app, task_id, worker_headers, artifact_id, payload,
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=30) for future in futures]

    assert upload_calls == 2
    assert fault_state["injected"] is True
    assert [response.status_code for response in responses] == [201, 201], [
        response.text for response in responses
    ]
    results = [response.json()["results"][0] for response in responses]
    assert results[0]["assetVersionId"] == results[1]["assetVersionId"]
    assert results[0]["mediaId"] == results[1]["mediaId"]
    _assert_g70_g71_postgres_single_ingest(sessions, task_id, payload)
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_duplicate_release_db_commit_ack_lost_after_commit_is_idempotent(
    postgres_context, monkeypatch
):
    """A durable release receipt must prevent a second decrement after a lost DB commit ACK."""
    _run_g70_g71_duplicate_release_commit_fault(
        postgres_context, monkeypatch, "after_commit"
    )


def test_g70_g71_postgres_duplicate_release_db_commit_failure_before_commit_retries_safely(
    postgres_context, monkeypatch
):
    """A pre-commit failure must retry the same release receipt and decrement exactly once."""
    _run_g70_g71_duplicate_release_commit_fault(
        postgres_context, monkeypatch, "before_commit"
    )


def test_g76_postgres_owner_cancel_during_artifact_upload_keeps_terminal_state(postgres_context, monkeypatch):
    """Owner cancel commits while sidecar upload is blocked; late intake must not overwrite CANCELED."""
    owner_client, sessions, app = postgres_context
    task_id = _create_task(owner_client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        project_id = task.project_id
        tenant_id = task.tenant_id

    worker_id = "worker-g76-cancel-race"
    upstream_id = "g76-cancel-race-upstream"
    artifact_id = "g76-cancel-race-artifact"
    claimed = owner_client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    epoch = claimed.json()["leaseEpoch"]
    assert claimed.json()["taskId"] == task_id
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(epoch),
    }
    running = owner_client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running", "upstreamJobId": upstream_id},
    )
    assert running.status_code == 200, running.text
    ingesting = owner_client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95, "message": "synthetic ingesting",
            "upstreamJobId": upstream_id, "providerArtifactId": artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    payload = b"g76-postgres-cancel-race-artifact"
    upload_started = Event()
    release_upload = Event()

    def blocked_upload(_project_id, filename, content_type, stream, *, idempotency_key, content_sha256):
        observed = stream.read()
        assert observed == payload
        assert idempotency_key == f"generation:{task_id}"
        assert content_sha256 == hashlib.sha256(observed).hexdigest()
        media_id = _expected_media_id(project_id, idempotency_key)
        upload_started.set()
        if not release_upload.wait(timeout=12):
            raise RuntimeError("synthetic blocked upload timed out")
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(observed),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }

    monkeypatch.setattr(app.state.video_use, "upload_media", blocked_upload)

    def send_artifact_intake():
        with TestClient(app) as worker_client:
            return worker_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers=worker_headers,
                data={"providerArtifactId": artifact_id},
                files={"file": ("synthetic.mp4", payload, "video/mp4")},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send_artifact_intake)
        try:
            assert upload_started.wait(timeout=10), "artifact upload did not reach the sidecar"
            canceled = owner_client.post(
                f"/projects/{project_id}/generation-tasks/{task_id}/cancel"
            )
            assert canceled.status_code == 200, canceled.text
            assert canceled.json()["status"] == "CANCELED"
        finally:
            release_upload.set()
        response = future.result(timeout=15)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "TASK_CANCELED"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        assert stored.status == "CANCELED"
        assert stored.cancel_requested_at is not None
        assert stored.lease_owner is None
        assert stored.lease_expires_at is None
        assert stored.media_id == _expected_media_id(project_id, f"generation:{task_id}")
        assert stored.asset_version_id is None
        assert tenant.used_storage_bytes == 0
        orphan_event = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one()
        assert orphan_event.metadata_json["reasonCode"] == "TASK_CANCELED"
        assert orphan_event.metadata_json["orphanedMediaId"] == _expected_media_id(project_id, f"generation:{task_id}")
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert orphan_event.metadata_json["originalProviderArtifactId"] == artifact_id
        assert orphan_event.metadata_json["originalUpstreamJobId"] == upstream_id
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == _expected_media_id(project_id, f"generation:{task_id}"),
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 1
    app.state.moneyprinter.assert_not_called()

def test_g76_postgres_provider_artifact_id_cannot_change_during_upload(postgres_context, monkeypatch):
    """A same-lease INGESTING transition cannot replace the artifact being uploaded."""
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        project_id = task.project_id
        tenant_id = task.tenant_id

    worker_id = "worker-g76-artifact-id-race"
    upstream_id = "g76-artifact-id-race-upstream"
    original_artifact_id = "g76-artifact-original"
    replacement_artifact_id = "g76-artifact-replacement"
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    epoch = claimed.json()["leaseEpoch"]
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(epoch),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running", "upstreamJobId": upstream_id},
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95, "message": "synthetic ingesting",
            "upstreamJobId": upstream_id, "providerArtifactId": original_artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    payload = b"g76-provider-artifact-id-race"
    upload_started = Event()
    release_upload = Event()

    def blocked_upload(_project_id, filename, content_type, stream, *, idempotency_key, content_sha256):
        observed = stream.read()
        assert observed == payload
        assert idempotency_key == f"generation:{task_id}"
        assert content_sha256 == hashlib.sha256(observed).hexdigest()
        media_id = _expected_media_id(project_id, idempotency_key)
        upload_started.set()
        if not release_upload.wait(timeout=12):
            raise RuntimeError("synthetic blocked upload timed out")
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(observed),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }

    monkeypatch.setattr(app.state.video_use, "upload_media", blocked_upload)

    def send_artifact_intake():
        with TestClient(app) as worker_client:
            return worker_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers=worker_headers,
                data={"providerArtifactId": original_artifact_id},
                files={"file": ("synthetic.mp4", payload, "video/mp4")},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send_artifact_intake)
        try:
            assert upload_started.wait(timeout=10), "artifact upload did not reach the sidecar"
            rejected = client.post(
                f"/internal/generation-tasks/{task_id}/transition",
                headers=worker_headers,
                json={
                    "status": "INGESTING", "progress": 96, "message": "conflicting artifact receipt",
                    "upstreamJobId": upstream_id, "providerArtifactId": replacement_artifact_id,
                },
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["detail"]["code"] == "PROVIDER_ARTIFACT_ID_IMMUTABLE"

            # Defense-in-depth: simulate a separate writer changing the row while
            # the sidecar request is in flight. The post-upload row lock must reject
            # the stale artifact and retain its exact orphan evidence.
            with sessions() as db:
                stored = db.get(DBGenerationTask, task_id)
                stored.provider_artifact_id = replacement_artifact_id
                db.commit()
        finally:
            release_upload.set()
        response = future.result(timeout=15)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_ID_MISMATCH"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        assert stored.status == "INGESTING"
        assert stored.provider_artifact_id == replacement_artifact_id
        assert stored.media_id is None
        assert stored.asset_version_id is None
        assert tenant.used_storage_bytes == 0
        orphan_event = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one()
        assert orphan_event.metadata_json["reasonCode"] == "ARTIFACT_ID_MISMATCH"
        assert orphan_event.metadata_json["orphanedMediaId"] == _expected_media_id(project_id, f"generation:{task_id}")
        assert orphan_event.metadata_json["originalProviderArtifactId"] == original_artifact_id
        assert orphan_event.metadata_json["currentTaskProviderArtifactId"] == replacement_artifact_id
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == _expected_media_id(project_id, f"generation:{task_id}"),
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
    app.state.moneyprinter.assert_not_called()



def test_g76_postgres_owner_kill_switch_during_artifact_upload_quarantines_late_artifact(postgres_context, monkeypatch):
    """Owner emergency stop committed during upload must quarantine the late artifact."""
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        project_id = task.project_id
        tenant_id = task.tenant_id

    worker_id = "worker-g76-kill-switch-race"
    upstream_id = "g76-kill-switch-race-upstream"
    artifact_id = "g76-kill-switch-race-artifact"
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    epoch = claimed.json()["leaseEpoch"]
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(epoch),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running", "upstreamJobId": upstream_id},
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95, "message": "synthetic ingesting",
            "upstreamJobId": upstream_id, "providerArtifactId": artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    payload = b"g76-postgres-kill-switch-race-artifact"
    upload_started = Event()
    release_upload = Event()

    def blocked_upload(_project_id, filename, content_type, stream, *, idempotency_key, content_sha256):
        observed = stream.read()
        assert observed == payload
        assert idempotency_key == f"generation:{task_id}"
        assert content_sha256 == hashlib.sha256(observed).hexdigest()
        media_id = _expected_media_id(project_id, idempotency_key)
        upload_started.set()
        if not release_upload.wait(timeout=12):
            raise RuntimeError("synthetic blocked upload timed out")
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(observed),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }

    monkeypatch.setattr(app.state.video_use, "upload_media", blocked_upload)

    def send_artifact_intake():
        with TestClient(app) as worker_client:
            return worker_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers=worker_headers,
                data={"providerArtifactId": artifact_id},
                files={"file": ("synthetic.mp4", payload, "video/mp4")},
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(send_artifact_intake)
        try:
            assert upload_started.wait(timeout=10), "artifact upload did not reach the sidecar"
            stopped = client.post(
                "/generation/providers/moneyprinter/kill-switch",
                json={"disabled": True, "reasonCode": "G76_TEST_KILL_SWITCH"},
            )
            assert stopped.status_code == 200, stopped.text
        finally:
            release_upload.set()
        response = future.result(timeout=15)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PROVIDER_EMERGENCY_STOPPED"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert circuit.state == "DISABLED"
        assert stored.status == "INGESTING"
        assert stored.asset_version_id is None
        assert tenant.used_storage_bytes == 0
        orphan_event = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one()
        assert orphan_event.metadata_json["reasonCode"] == "PROVIDER_EMERGENCY_STOPPED"
        assert orphan_event.metadata_json["orphanedMediaId"] == _expected_media_id(project_id, f"generation:{task_id}")
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert orphan_event.metadata_json["originalProviderArtifactId"] == artifact_id
        assert orphan_event.metadata_json["originalUpstreamJobId"] == upstream_id
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == _expected_media_id(project_id, f"generation:{task_id}"),
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()



def test_g76_postgres_kill_switch_after_missing_circuit_select_is_serialized(postgres_context, monkeypatch):
    """A missing circuit row must be materialized and locked before artifact commit."""
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id
        project_id = task.project_id
        assert db.execute(
            select(func.count(DBGenerationCircuitState.id)).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one() == 0

    worker_id = "worker-g76-missing-circuit-race"
    upstream_id = "g76-missing-circuit-race-upstream"
    artifact_id = "g76-missing-circuit-race-artifact"
    claimed = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claimed.status_code == 200, claimed.text
    epoch = claimed.json()["leaseEpoch"]
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(epoch),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={"status": "RUNNING", "progress": 10, "message": "synthetic running", "upstreamJobId": upstream_id},
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95, "message": "synthetic ingesting",
            "upstreamJobId": upstream_id, "providerArtifactId": artifact_id,
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    # The claim path now materializes the circuit row. Delete it only in this
    # disposable PostgreSQL fixture to recreate the absent-row state at
    # artifact finalization, which is the race this test is intended to prove.
    with sessions() as db:
        deleted = db.execute(
            text(
                "DELETE FROM generation_circuit_states "
                "WHERE tenant_id = :tenant_id AND provider = 'moneyprinter'"
            ),
            {"tenant_id": tenant_id},
        )
        assert deleted.rowcount == 1
        db.commit()

    payload = b"g76-postgres-missing-circuit-race-artifact"
    upload_started = Event()
    release_upload = Event()
    circuit_select_finished = Event()
    release_circuit_select = Event()
    owner_request_started = Event()
    worker_thread = {"ident": None}

    def blocked_upload(_project_id, filename, content_type, stream, *, idempotency_key, content_sha256):
        observed = stream.read()
        assert observed == payload
        assert idempotency_key == f"generation:{task_id}"
        assert content_sha256 == hashlib.sha256(observed).hexdigest()
        media_id = _expected_media_id(project_id, idempotency_key)
        worker_thread["ident"] = get_ident()
        upload_started.set()
        if not release_upload.wait(timeout=12):
            raise RuntimeError("synthetic blocked upload timed out")
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(observed),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }

    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if (
            worker_thread["ident"] == get_ident()
            and "generation_circuit_states" in statement.lower()
            and "for update" in statement.lower()
        ):
            circuit_select_finished.set()
            if not release_circuit_select.wait(timeout=12):
                raise RuntimeError("synthetic circuit-select barrier timed out")

    def send_artifact_intake():
        with TestClient(app) as worker_client:
            return worker_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers=worker_headers,
                data={"providerArtifactId": artifact_id},
                files={"file": ("synthetic.mp4", payload, "video/mp4")},
            )

    def send_owner_kill_switch():
        with TestClient(app) as owner_client:
            login = owner_client.post(
                "/auth/login",
                json={"email": "g76-postgres-owner@example.com", "password": OWNER_PASSWORD},
            )
            assert login.status_code == 200, login.text
            owner_client.headers.update({"X-Aether-CSRF": "1"})
            owner_request_started.set()
            return owner_client.post(
                "/generation/providers/moneyprinter/kill-switch",
                json={"disabled": True, "reasonCode": "G76_TEST_MISSING_CIRCUIT_RACE"},
            )

    monkeypatch.setattr(app.state.video_use, "upload_media", blocked_upload)
    engine = sessions.kw["bind"]
    event.listen(engine, "after_cursor_execute", after_cursor_execute)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            artifact_future = pool.submit(send_artifact_intake)
            if not upload_started.wait(timeout=10):
                if artifact_future.done():
                    early_response = artifact_future.result(timeout=1)
                    pytest.fail(
                        "artifact intake returned before sidecar: "
                        f"HTTP {early_response.status_code} {early_response.text}"
                    )
                pytest.fail("artifact upload did not reach the sidecar and request is still pending")
            release_upload.set()
            assert circuit_select_finished.wait(timeout=10), "final circuit SELECT FOR UPDATE was not observed"

            # Artifact finalization has atomically materialized and locked the
            # previously absent circuit row. Owner stop must wait for that commit.
            owner_future = pool.submit(send_owner_kill_switch)
            assert owner_request_started.wait(timeout=10), "Owner kill-switch request did not start"
            assert _wait_until_postgres_reports_lock_wait(
                sessions, table_name="generation_circuit_states", timeout_seconds=8.0
            ), "Owner kill-switch did not wait on the circuit-row serialization lock"
            assert not owner_future.done()

            release_circuit_select.set()
            response = artifact_future.result(timeout=15)
            stopped = owner_future.result(timeout=15)
    finally:
        release_upload.set()
        release_circuit_select.set()
        event.remove(engine, "after_cursor_execute", after_cursor_execute)

    assert response.status_code == 201, response.text
    assert stopped.status_code == 200, stopped.text
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        assert circuit.state == "DISABLED"
        assert stored.status == "RIGHTS_BLOCKED"
        assert stored.asset_version_id is not None
        assert tenant.used_storage_bytes == len(payload)
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == _expected_media_id(project_id, f"generation:{task_id}"),
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()


def test_g73_postgres_keyset_claim_scans_past_205_foreign_wrong_mode_rows(postgres_context):
    """PostgreSQL keyset paging must reach an eligible task beyond 205 foreign wrong-mode rows."""
    client, sessions, app = postgres_context
    eligible_task_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        eligible = db.get(DBGenerationTask, eligible_task_id)
        assert eligible is not None
        eligible_tenant_id = eligible.tenant_id
        first_created = eligible.created_at - datetime.timedelta(days=1)

        foreign_tenant = DBTenant(
            id="g73-pg-foreign-tenant",
            name="G73 PostgreSQL Foreign Tenant",
            slug="g73-pg-foreign-tenant",
            project_quota=50,
            storage_quota_bytes=1024**3,
            used_storage_bytes=0,
            concurrent_render_quota=2,
            monthly_render_seconds_quota=3600,
            render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"),
            created_at=now,
            updated_at=now,
        )
        foreign_owner = DBUser(
            id="g73-pg-foreign-owner",
            tenant_id=foreign_tenant.id,
            email="g73-pg-foreign-owner@example.com",
            display_name="G73 PostgreSQL Foreign Owner",
            password_hash="synthetic-fixture-not-used-for-login",
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        foreign_project = DBProject(
            id="g73-pg-foreign-project",
            tenant_id=foreign_tenant.id,
            owner_id=foreign_owner.id,
            name="G73 PostgreSQL foreign wrong-mode backlog",
            timeline={"version": "1.1", "output": None, "tracks": []},
            materials=[],
            revision=1,
            created_at=now,
            updated_at=now,
        )
        ineligible_rows = []
        for idx in range(205):
            created_at = first_created + datetime.timedelta(microseconds=idx)
            ineligible_rows.append(DBGenerationTask(
                id=str(uuid.uuid4()),
                tenant_id=foreign_tenant.id,
                project_id=foreign_project.id,
                requested_by=foreign_owner.id,
                provider="moneyprinter",
                status="QUEUED",
                progress=0,
                message="synthetic foreign-tenant wrong-mode PostgreSQL row",
                request_json={"syntheticFixtureOnly": True},
                request_hash=f"g73-pg-foreign-fixture-{idx}",
                capability_snapshot_json={"mode": "moneyprinter"},
                capability_snapshot_hash="0" * 64,
                idempotency_key=f"g73-pg-foreign-idempotency-{idx}",
                upstream_job_id=None,
                provider_artifact_id=None,
                media_id=None,
                asset_version_id=None,
                attempts=1,
                max_attempts=1,
                lease_owner=None,
                lease_epoch=0,
                circuit_probe_epoch=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                error_code=None,
                error_message=None,
                created_at=created_at,
                updated_at=created_at,
            ))
        # Persist synthetic FK parents in dependency order before the 205 task rows.
        # Scalar FK values alone do not give SQLAlchemy's unit of work relationship ordering.
        db.add(foreign_tenant)
        db.flush()
        db.add(foreign_owner)
        db.flush()
        db.add(foreign_project)
        db.flush()
        db.add_all(ineligible_rows)
        db.commit()

    worker_id = "g73-pg-keyset-worker"
    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
        )

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == eligible_task_id
    assert claimed.json()["providerMode"] == "deterministic-fake"

    with sessions() as db:
        eligible = db.get(DBGenerationTask, eligible_task_id)
        assert eligible is not None
        assert eligible.tenant_id == eligible_tenant_id
        assert eligible.status == "SUBMITTING"
        assert eligible.lease_owner == worker_id
        assert eligible.lease_epoch == 1

        foreign_queued = db.execute(
            select(func.count(DBGenerationTask.id)).where(
                DBGenerationTask.tenant_id == "g73-pg-foreign-tenant",
                DBGenerationTask.status == "QUEUED",
                DBGenerationTask.lease_owner.is_(None),
            )
        ).scalar_one()
        assert foreign_queued == 205

        foreign_claim_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id.in_(
                    select(DBGenerationTask.id).where(
                        DBGenerationTask.tenant_id == "g73-pg-foreign-tenant"
                    )
                ),
                DBGenerationEvent.event_type == "TASK_CLAIMED",
            )
        ).scalar_one()
        assert foreign_claim_events == 0

    app.state.moneyprinter.assert_not_called()


def test_g75_postgres_half_open_probe_is_single_claim_under_two_workers(postgres_context):
    """Two concurrent PostgreSQL claimers must not acquire the same tenant's HALF_OPEN probe."""
    client, sessions, app = postgres_context
    task_ids = [_create_task(client), _create_task(client)]
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        first_task = db.get(DBGenerationTask, task_ids[0])
        assert first_task is not None
        tenant_id = first_task.tenant_id
        db.add(DBGenerationCircuitState(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider="moneyprinter",
            state="HALF_OPEN",
            failure_timestamps_json=[],
            opened_at=now - datetime.timedelta(minutes=2),
            cooldown_until=now - datetime.timedelta(seconds=1),
            half_open_task_id=None,
            probe_epoch=7,
            half_open_lease_epoch=None,
            half_open_issuer=None,
            disabled_reason_code=None,
            updated_at=now,
        ))
        db.commit()

    barrier = Barrier(2)

    def concurrent_claim(worker_id: str):
        with TestClient(app) as worker_client:
            barrier.wait(timeout=10)
            response = worker_client.post(
                "/internal/generation-tasks/claim",
                headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
            )
            return worker_id, response

    worker_ids = ("g75-pg-worker-a", "g75-pg-worker-b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(concurrent_claim, worker_id) for worker_id in worker_ids]
        results = [future.result(timeout=20) for future in futures]

    claimed_results = [(worker_id, response) for worker_id, response in results if response.status_code == 200]
    empty_results = [(worker_id, response) for worker_id, response in results if response.status_code == 204]
    assert len(claimed_results) == 1, [(worker, response.status_code, response.text) for worker, response in results]
    assert len(empty_results) == 1, [(worker, response.status_code, response.text) for worker, response in results]

    winner_id, winner_response = claimed_results[0]
    claimed_task_id = winner_response.json()["taskId"]
    assert claimed_task_id in task_ids
    assert winner_response.json()["leaseEpoch"] == 1
    assert winner_response.json()["circuitProbeEpoch"] == 8

    with sessions() as db:
        persisted_tasks = [db.get(DBGenerationTask, task_id) for task_id in task_ids]
        claimed_task = next(task for task in persisted_tasks if task.id == claimed_task_id)
        unclaimed_task = next(task for task in persisted_tasks if task.id != claimed_task_id)
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        claimed_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id.in_(task_ids),
                DBGenerationEvent.event_type == "TASK_CLAIMED",
            )
        ).scalar_one()

        assert claimed_task.status == "SUBMITTING"
        assert claimed_task.lease_owner == winner_id
        assert claimed_task.lease_epoch == 1
        assert claimed_task.circuit_probe_epoch == 8
        assert unclaimed_task.status == "QUEUED"
        assert unclaimed_task.lease_owner is None
        assert unclaimed_task.lease_epoch == 0
        assert circuit.state == "HALF_OPEN"
        assert circuit.probe_epoch == 8
        assert circuit.half_open_task_id == claimed_task_id
        assert circuit.half_open_lease_epoch == claimed_task.lease_epoch
        assert circuit.half_open_issuer == winner_id
        assert claimed_events == 1

    app.state.moneyprinter.assert_not_called()

def test_g76_postgres_owner_stop_serializes_with_worker_claim_when_circuit_row_missing(postgres_context):
    """A missing circuit row must not let a Worker claim race past Owner emergency stop."""
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id
        assert db.execute(
            select(func.count(DBGenerationCircuitState.id)).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one() == 0

    owner_insert_finished = Event()
    release_owner_insert = Event()
    owner_request_active = Event()

    def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        # TestClient dispatches the ASGI endpoint on its own portal thread;
        # the outer caller thread is not the SQL execution thread. Scope the
        # barrier to the Owner request window instead of comparing thread IDs.
        if (
            owner_request_active.is_set()
            and "insert into generation_circuit_states" in statement.lower()
        ):
            owner_insert_finished.set()
            if not release_owner_insert.wait(timeout=12):
                raise RuntimeError("synthetic Owner circuit insert barrier timed out")

    def send_owner_stop():
        with TestClient(app) as owner_client:
            login = owner_client.post(
                "/auth/login",
                json={"email": "g76-postgres-owner@example.com", "password": OWNER_PASSWORD},
            )
            assert login.status_code == 200, login.text
            owner_client.headers.update({"X-Aether-CSRF": "1"})
            owner_request_active.set()
            return owner_client.post(
                "/generation/providers/moneyprinter/kill-switch",
                json={"disabled": True, "reasonCode": "G76_TEST_MISSING_CIRCUIT_CLAIM_RACE"},
            )

    def send_claim():
        with TestClient(app) as worker_client:
            return worker_client.post(
                "/internal/generation-tasks/claim",
                headers={
                    "X-Worker-Token": WORKER_TOKEN,
                    "X-Worker-Id": "worker-g76-missing-circuit-claim-race",
                },
            )

    engine = sessions.kw["bind"]
    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            owner_future = pool.submit(send_owner_stop)
            assert owner_insert_finished.wait(timeout=10), "Owner circuit row was not created"
            claim_future = pool.submit(send_claim)
            assert _wait_until_postgres_reports_lock_wait(
                sessions, table_name="generation_circuit_states", timeout_seconds=8.0
            ), "Worker claim did not serialize on the missing-row upsert"
            assert not claim_future.done(), "Worker claim escaped while Owner stop held the circuit row"
            release_owner_insert.set()
            stopped = owner_future.result(timeout=15)
            claimed = claim_future.result(timeout=15)
    finally:
        release_owner_insert.set()
        event.remove(engine, "after_cursor_execute", after_cursor_execute)

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["killSwitch"]["disabled"] is True
    assert claimed.status_code == 204, claimed.text
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        assert circuit.state == "DISABLED"
        assert stored.status == "QUEUED"
        assert stored.lease_owner is None
        assert stored.lease_expires_at is None
        assert stored.lease_epoch == 0
        assert db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "TASK_CLAIMED",
            )
        ).scalar_one() == 0
        assert db.execute(
            select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )
        ).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()



def test_g70_g71_postgres_audit_abrupt_request_exit_after_sidecar_return_does_not_leak_storage_hold(
    postgres_context, monkeypatch
):
    """An in-process BaseException after Sidecar upload must not leak its quota hold.

    The exception bypasses the ordinary Exception failure path. This is not
    an operating-system process kill or durable crash-recovery proof.
    """
    class SimulatedAbruptRequestExit(BaseException):
        pass

    owner_client, sessions, app = postgres_context
    task_id, worker_headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = b"synthetic-g70-g71-abrupt-request-exit-after-sidecar-return"

    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id
        tenant = db.get(DBTenant, tenant_id)
        assert tenant is not None
        initial_storage_bytes = tenant.used_storage_bytes

    original_upload = app.state.video_use.upload_media
    upload_calls = 0

    def upload_then_abort_request(*args, **kwargs):
        nonlocal upload_calls
        upload_calls += 1
        original_upload(*args, **kwargs)
        raise SimulatedAbruptRequestExit("synthetic abrupt request exit after Sidecar returned")

    monkeypatch.setattr(app.state.video_use, "upload_media", upload_then_abort_request)
    intake_route = next(
        route for route in app.routes
        if getattr(route, "path", None) == "/internal/generation-tasks/{task_id}/artifact-intake"
    )
    interrupted = False
    with sessions() as db:
        upload = UploadFile(
            file=BytesIO(payload),
            filename="synthetic-g70-g71.mp4",
            headers=Headers({"content-type": "video/mp4"}),
        )
        try:
            intake_route.endpoint(
                task_id,
                providerArtifactId=artifact_id,
                file=upload,
                x_worker_token=WORKER_TOKEN,
                x_worker_id=worker_headers["X-Worker-Id"],
                x_worker_lease_epoch=int(worker_headers["X-Worker-Lease-Epoch"]),
                db=db,
            )
        except SimulatedAbruptRequestExit:
            interrupted = True
        else:
            pytest.fail("synthetic abrupt request interruption did not reach the intake endpoint")

    assert upload_calls == 1
    assert interrupted is True

    with sessions() as db:
        interrupted_task = db.get(DBGenerationTask, task_id)
        interrupted_tenant = db.get(DBTenant, tenant_id)
        assert interrupted_task is not None
        assert interrupted_tenant is not None
        assert interrupted_task.status == "INGESTING"
        assert interrupted_task.asset_version_id is None
        assert interrupted_tenant.used_storage_bytes == initial_storage_bytes
        releases = list(db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalars())
        assert len(releases) == 1
        assert releases[0].metadata_json["reasonCode"] == "UPLOAD_INTERRUPTED"
        assert releases[0].metadata_json["reservedStorageBytes"] == len(payload)

    monkeypatch.setattr(app.state.video_use, "upload_media", original_upload)
    retry = _post_g70_g71_duplicate_intake(
        app, task_id, worker_headers, artifact_id, payload
    )
    assert retry.status_code == 201, retry.text

    with sessions() as db:
        completed_task = db.get(DBGenerationTask, task_id)
        completed_tenant = db.get(DBTenant, tenant_id)
        assert completed_task is not None
        assert completed_tenant is not None
        assets = list(db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.project_id == completed_task.project_id,
                DBAssetVersion.media_id == completed_task.media_id,
            )
        ).scalars())
        events = list(db.execute(
            select(DBGenerationEvent).where(
                DBGenerationEvent.generation_task_id == task_id
            )
        ).scalars())
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task_id
            )
        ).scalars())

        assert completed_task.status == "RIGHTS_BLOCKED"
        assert completed_task.attempts == 1
        assert completed_task.asset_version_id is not None
        assert len(assets) == 1
        assert assets[0].sha256 == hashlib.sha256(payload).hexdigest()
        assert sum(row.event_type == "ARTIFACT_INGESTED" for row in events) == 1
        assert sum(row.event_type == "STORAGE_RESERVATION_RELEASED" for row in events) == 1
        assert sum(row.kind == "RESERVED" for row in usage) == 1
        assert sum(row.kind == "SETTLED" for row in usage) == 1
        assert sum(row.kind == "RELEASED" for row in usage) == 0

        expected_storage_bytes = initial_storage_bytes + len(payload)
        assert completed_tenant.used_storage_bytes == expected_storage_bytes, (
            "Aborted request must compensate its own hold, and retry must reserve "
            "exactly once for the single durable media object."
        )

    app.state.moneyprinter.assert_not_called()

@pytest.mark.parametrize("rotate_lease", [False, True])
def test_g70_g71_postgres_sigkill_after_sidecar_write_replay_storage_accounting(
    postgres_context, rotate_lease,
):
    """OS SIGKILL: both lease generations must fail closed on a durable orphan.

    FAKE file-backed Sidecar + disposable PostgreSQL only. A disconnected
    liveness lock is a crash signal, not authority to refund or auto-adopt.
    """
    import signal
    import subprocess
    import sys
    import tempfile

    if sys.platform != "linux":
        pytest.skip("real SIGKILL requires Linux disposable runner")

    owner_client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = b"g70-g71-real-os-process-kill-after-fake-sidecar-write"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id = task.tenant_id
        before_bytes = db.get(DBTenant, tenant_id).used_storage_bytes

    child_script = r'''
import hashlib
import os
import time
from io import BytesIO
from pathlib import Path
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy.orm import sessionmaker
from app.database import build_engine
from app.main import create_app
from test_g76_postgres_concurrency import FakeMediaStore

class PersistThenBlockFakeSidecar(FakeMediaStore):
    def upload_media(self, project_id, filename, content_type, stream, *, idempotency_key=None, content_sha256=None):
        payload = stream.read()
        assert hashlib.sha256(payload).hexdigest() == content_sha256
        Path(os.environ["AETHER_TEST_CRASH_MEDIA"]).write_bytes(payload)
        Path(os.environ["AETHER_TEST_CRASH_READY"]).write_text("persisted-after-hold-commit")
        while True:
            time.sleep(0.2)

engine = build_engine(os.environ["AETHER_TEST_POSTGRES_URL"])
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
    video_use_adapter=PersistThenBlockFakeSidecar(),
    bootstrap_admin_password=os.environ["AETHER_TEST_CHILD_OWNER_PASSWORD"],
    bootstrap_admin_email="g76-postgres-owner@example.com",
    worker_token=os.environ["AETHER_TEST_CHILD_WORKER_TOKEN"],
    cookie_secure=False,
    enforce_csrf=True,
    generation_provider_mode="deterministic-fake",
)
route = next(
    x for x in app.routes
    if getattr(x, "path", None) == "/internal/generation-tasks/{task_id}/artifact-intake"
)
with sessions() as db:
    route.endpoint(
        os.environ["AETHER_TEST_CRASH_TASK"],
        providerArtifactId=os.environ["AETHER_TEST_CRASH_ARTIFACT"],
        file=UploadFile(
            file=BytesIO(b"g70-g71-real-os-process-kill-after-fake-sidecar-write"),
            filename="synthetic-g70-g71.mp4",
            headers=Headers({"content-type": "video/mp4"}),
        ),
        x_worker_token=os.environ["AETHER_TEST_CHILD_WORKER_TOKEN"],
        x_worker_id=os.environ["AETHER_TEST_CRASH_WORKER"],
        x_worker_lease_epoch=int(os.environ["AETHER_TEST_CRASH_EPOCH"]),
        db=db,
    )
'''
    with tempfile.TemporaryDirectory(prefix="aether-g70-oskill-fake-") as tempdir:
        ready_file = Path(tempdir, "after-sidecar.ready")
        media_file = Path(tempdir, "fake-sidecar.media")
        child_env = os.environ.copy()
        child_env.update({
            "AETHER_TEST_CRASH_READY": str(ready_file),
            "AETHER_TEST_CRASH_MEDIA": str(media_file),
            "AETHER_TEST_CRASH_TASK": task_id,
            "AETHER_TEST_CRASH_ARTIFACT": artifact_id,
            "AETHER_TEST_CRASH_WORKER": headers["X-Worker-Id"],
            "AETHER_TEST_CRASH_EPOCH": headers["X-Worker-Lease-Epoch"],
            "AETHER_TEST_CHILD_WORKER_TOKEN": WORKER_TOKEN,
            "AETHER_TEST_CHILD_OWNER_PASSWORD": OWNER_PASSWORD,
        })
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_script],
            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready_file.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready_file.exists():
                if child.poll() is None:
                    pytest.fail("child did not reach fake sidecar before timeout")
                _, err = child.communicate(timeout=5)
                pytest.fail(f"OS-kill setup failed before Sidecar: {err[-1200:]}")
            assert child.poll() is None, "API child exited before SIGKILL"
            assert media_file.read_bytes() == payload
            os.kill(child.pid, signal.SIGKILL)
            assert child.wait(timeout=10) == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.communicate(timeout=5)

        # Verify committed reservation with a fresh independent PostgreSQL engine.
        independent_engine = build_engine(os.environ["AETHER_TEST_POSTGRES_URL"])
        try:
            with Session(independent_engine) as db:
                tenant = db.get(DBTenant, tenant_id)
                assert tenant is not None
                assert tenant.used_storage_bytes == before_bytes + len(payload)
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
                )).scalar_one() == 0
                # Native read-only crash evidence: the original committed
                # debit must be attributable to one trusted, typed OPENED row.
                # A row count alone cannot prove the issuer, artifact identity,
                # lease epoch, bytes or durable held-quota binding.
                opened_rows = db.execute(select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
                )).scalars().all()
                assert len(opened_rows) == 1
                opened = opened_rows[0]
                assert (opened.actor_type, opened.actor_id) == ("SYSTEM", "api")
                stored_task = db.get(DBGenerationTask, task_id)
                assert stored_task is not None
                assert stored_task.tenant_id == tenant_id
                assert stored_task.asset_version_id is None
                assert stored_task.status == "INGESTING"
                meta = opened.metadata_json
                assert isinstance(meta, dict)
                assert meta.get("workerId") == headers["X-Worker-Id"]
                assert type(meta.get("leaseEpoch")) is int
                assert meta["leaseEpoch"] == int(headers["X-Worker-Lease-Epoch"])
                assert meta.get("providerArtifactId") == artifact_id
                assert meta.get("mediaId") == _expected_media_id(
                    stored_task.project_id, f"generation:{task_id}"
                )
                assert meta.get("sha256") == hashlib.sha256(payload).hexdigest()
                assert type(meta.get("reservedStorageBytes")) is int
                assert meta["reservedStorageBytes"] == len(payload)
                assert type(meta.get("livenessLockKey")) is int
                assert meta["livenessLockKey"] >= 0
                assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                    DBGenerationUsageEntry.task_id == task_id,
                    DBGenerationUsageEntry.kind == "SETTLED",
                )).scalar_one() == 0
                assert media_file.read_bytes() == payload
        finally:
            independent_engine.dispose()

        # Before attempting normal replay, a changed artifact from the SAME
        # lease epoch must be rejected against the durable opened receipt.
        # Prior to this fence it could reserve a second quota hold and call
        # the Sidecar even though the original SIGKILL orphan is unresolved.
        # This is an actual OS-kill setup with a file-backed FAKE Sidecar;
        # it does not prove automatic orphan reconciliation.
        contradictory = _post_g70_g71_duplicate_intake(
            app, task_id, headers, artifact_id, payload + b"-changed-after-crash"
        )
        assert contradictory.status_code == 409, contradictory.text
        assert contradictory.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
        with sessions() as db:
            task = db.get(DBGenerationTask, task_id)
            tenant = db.get(DBTenant, tenant_id)
            assert task is not None and tenant is not None
            assert task.asset_version_id is None
            assert task.status == "INGESTING"
            assert tenant.used_storage_bytes == before_bytes + len(payload)
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
            )).scalar_one() == 1
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
            )).scalar_one() == 0
        assert media_file.read_bytes() == payload
        app.state.moneyprinter.assert_not_called()

        if rotate_lease:
            # A new worker generation can be granted after a dead API's
            # uncertain upload. The original hold cannot be silently reused
            # or reserved again by a different lease epoch.
            with sessions() as db:
                task = db.get(DBGenerationTask, task_id)
                assert task is not None
                task.lease_epoch += 1
                headers["X-Worker-Lease-Epoch"] = str(task.lease_epoch)
                db.commit()

        replay = _post_g70_g71_duplicate_intake(app, task_id, headers, artifact_id, payload)
        if rotate_lease:
            assert replay.status_code == 409, replay.text
            assert replay.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
            with sessions() as db:
                task = db.get(DBGenerationTask, task_id)
                tenant = db.get(DBTenant, tenant_id)
                assert task is not None and tenant is not None
                assert task.asset_version_id is None
                assert tenant.used_storage_bytes == before_bytes + len(payload)
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
                )).scalar_one() == 1
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
                )).scalar_one() == 0
            assert media_file.read_bytes() == payload
            return

        # A SIGKILL releases the PostgreSQL session-level liveness lock
        # even though the hold and FAKE Sidecar bytes remain durable.
        # Same-epoch replay must now refuse a second quota reservation.
        assert replay.status_code == 409, replay.text
        assert replay.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
        with sessions() as db:
            task = db.get(DBGenerationTask, task_id)
            tenant = db.get(DBTenant, tenant_id)
            assert task is not None and tenant is not None
            assert task.status == "INGESTING"
            assert task.asset_version_id is None
            assert tenant.used_storage_bytes == before_bytes + len(payload)
            holds = db.execute(select(DBGenerationEvent).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
            )).scalars().all()
            assert len(holds) == 1
            assert type(holds[0].metadata_json.get("livenessLockKey")) is int
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
            )).scalar_one() == 0
        assert media_file.read_bytes() == payload
        app.state.moneyprinter.assert_not_called()

def test_g73_postgres_poisoned_queue_does_not_starve_healthy_fake_claim(postgres_context):
    """A corrupt persisted JSON payload must not starve independent FAKE Worker claims.

    Runs on disposable native PostgreSQL; no Provider, production data, or
    financial/reconciliation writes are authorized by this test.
    """
    client, sessions, app = postgres_context
    bad_snapshot_id = _create_task(client)
    bad_request_id = _create_task(client)
    bad_generic_request_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        first = db.get(DBGenerationTask, bad_snapshot_id)
        second = db.get(DBGenerationTask, bad_request_id)
        third = db.get(DBGenerationTask, bad_generic_request_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert all(row is not None for row in (first, second, third, healthy))
        first.created_at = now - datetime.timedelta(minutes=4)
        first.capability_snapshot_json = ["legacy", "bad-snapshot"]
        second.created_at = now - datetime.timedelta(minutes=3)
        second.provider = "deterministic-fake-talking-head"
        second.request_json = ["legacy", "bad-talking-head-request"]
        third.created_at = now - datetime.timedelta(minutes=2)
        third.request_json = ["legacy", "bad-generic-fake-request"]
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-postgres-poison-safe-worker",
            },
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id
    assert claimed.json()["leaseEpoch"] == 1

    with sessions() as db:
        for poisoned_id in (bad_snapshot_id, bad_request_id, bad_generic_request_id):
            poisoned = db.get(DBGenerationTask, poisoned_id)
            assert poisoned.status == "QUEUED"
            assert poisoned.lease_owner is None
            assert poisoned.lease_epoch == 0
            assert db.execute(
                select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == poisoned_id,
                    DBGenerationEvent.event_type == "TASK_CLAIMED",
                )
            ).scalar_one() == 0
        eligible = db.get(DBGenerationTask, healthy_id)
        assert eligible.status == "SUBMITTING"
        assert eligible.lease_owner == "g73-postgres-poison-safe-worker"
    app.state.moneyprinter.assert_not_called()


def test_g73_postgres_missing_current_attempt_does_not_starve_healthy_claim(postgres_context):
    """Native PostgreSQL must skip an orphan attempt pointer without leasing it."""
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        damaged.attempts += 1
        damaged.created_at = now - datetime.timedelta(minutes=2)
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        result = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-postgres-missing-attempt-safe-worker",
            },
        )
    assert result.status_code == 200, result.text
    assert result.json()["taskId"] == healthy_id

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged.status == "QUEUED"
        assert damaged.lease_owner is None
        assert damaged.lease_epoch == 0
        assert db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == damaged_id,
                DBGenerationEvent.event_type == "TASK_CLAIMED",
            )
        ).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-postgres-missing-attempt-safe-worker"
    app.state.moneyprinter.assert_not_called()


def test_g73_postgres_terminal_attempt_is_not_reclaimed_from_queued_task(postgres_context):
    """Fail closed on an inconsistent QUEUED task/terminal attempt in native PG.

    Historical/corrupt rows must remain untouched for authorized reconciliation.
    A healthy FAKE-only task must not be starved; no real Provider is contacted.
    """
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        current = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == "QUEUED" and current.status == "QUEUED"
        current.status = "SUCCEEDED"
        damaged.created_at = now - datetime.timedelta(minutes=2)
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        result = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-terminal-attempt-safe-worker",
            },
        )
    assert result.status_code == 200, result.text
    assert result.json()["taskId"] == healthy_id

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        current = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == "QUEUED"
        assert damaged.lease_owner is None
        assert damaged.lease_epoch == 0
        assert current.status == "SUCCEEDED"
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == damaged_id,
            DBGenerationEvent.event_type == "TASK_CLAIMED",
        )).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-terminal-attempt-safe-worker"
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("terminal_status", ["SUCCEEDED", "UNKNOWN"])
def test_g73_postgres_active_task_terminal_attempt_is_quarantined(postgres_context, terminal_status):
    """A SUBMITTING task cannot re-lease a terminal/ambiguous current attempt.

    Native disposable PostgreSQL and FAKE-only Worker token; preserve disputed
    attempt/ledger for authorized reconciliation, without a Provider call.
    """
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        current = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        damaged.status = "SUBMITTING"
        damaged.created_at = now - datetime.timedelta(minutes=2)
        damaged.lease_owner = None
        damaged.lease_expires_at = None
        current.status = terminal_status
        current.submission_started_at = now - datetime.timedelta(minutes=2)
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={"X-Worker-Token": WORKER_TOKEN,
                     "X-Worker-Id": "g73-active-terminal-attempt-fence"},
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        current = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == "SUBMITTING"
        assert damaged.lease_owner is None
        assert damaged.lease_epoch == 0
        assert damaged.lease_expires_at is None
        assert current.status == terminal_status
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == damaged_id,
            DBGenerationEvent.event_type.in_(["TASK_CLAIMED", "LEASE_RECOVERED"]),
        )).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-active-terminal-attempt-fence"
    app.state.moneyprinter.assert_not_called()



@pytest.mark.parametrize(
    ("task_status", "attempt_status"),
    [
        ("SUBMITTING", "QUEUED"),
        ("RUNNING", "SUBMITTING"),
        ("INGESTING", "RUNNING"),
        ("SUBMITTING", "INVALID_LEGACY_STATUS"),
    ],
)
def test_g73_postgres_active_task_nonterminal_attempt_mismatch_is_quarantined(
    postgres_context, task_status, attempt_status,
):
    """A corrupted active task/attempt mismatch may not acquire a Worker lease.

    FAKE-only disposable PostgreSQL: preserve the disputed history and allow
    the later healthy task to progress. No reconciliation or refund is inferred.
    """
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        damaged.status = task_status
        damaged.created_at = now - datetime.timedelta(minutes=2)
        damaged.lease_owner = None
        damaged.lease_expires_at = None
        attempt.status = attempt_status
        attempt.submission_started_at = (
            None if attempt_status == "QUEUED"
            else now - datetime.timedelta(minutes=2)
        )
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={"X-Worker-Token": WORKER_TOKEN,
                     "X-Worker-Id": "g73-mismatched-attempt-safe-worker"},
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id
    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == task_status
        assert damaged.lease_owner is None
        assert damaged.lease_expires_at is None
        assert damaged.lease_epoch == 0
        assert attempt.status == attempt_status
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == damaged_id,
            DBGenerationEvent.event_type.in_(["TASK_CLAIMED", "LEASE_RECOVERED"]),
        )).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-mismatched-attempt-safe-worker"
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize(
    "durable_evidence",
    ["submission_started", "reconciled_submitted", "attempt_upstream", "task_upstream"],
)
def test_g73_postgres_queued_with_prior_submission_evidence_is_quarantined(
    postgres_context, durable_evidence,
):
    """A QUEUED legacy attempt cannot be resubmitted if prior submission is plausible.

    In disposable PostgreSQL only, keep its attempt, ledger and audit untouched;
    a subsequent healthy FAKE task must remain claimable. No real Provider runs.
    """
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == attempt.status == "QUEUED"
        assert attempt.reconciliation_state == "NOT_SUBMITTED"
        if durable_evidence == "submission_started":
            attempt.submission_started_at = now - datetime.timedelta(minutes=5)
        elif durable_evidence == "reconciled_submitted":
            attempt.reconciliation_state = "SUBMITTED"
        elif durable_evidence == "attempt_upstream":
            attempt.upstream_job_id = "legacy-upstream-123"
        else:
            damaged.upstream_job_id = "legacy-upstream-123"
        damaged.created_at = now - datetime.timedelta(minutes=2)
        healthy.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-queued-submission-proof-fence",
            },
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id
    assert claimed.json()["submissionConsumed"] is False

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == "QUEUED"
        assert damaged.lease_owner is None
        assert damaged.lease_expires_at is None
        assert damaged.lease_epoch == 0
        assert attempt.status == "QUEUED"
        if durable_evidence == "submission_started":
            assert attempt.submission_started_at is not None
        elif durable_evidence == "reconciled_submitted":
            assert attempt.reconciliation_state == "SUBMITTED"
        elif durable_evidence == "attempt_upstream":
            assert attempt.upstream_job_id == "legacy-upstream-123"
        else:
            assert damaged.upstream_job_id == "legacy-upstream-123"
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == damaged_id,
            DBGenerationEvent.event_type.in_(["TASK_CLAIMED", "LEASE_RECOVERED"]),
        )).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-queued-submission-proof-fence"
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize(
    "scenario",
    [
        "submitting_missing_submit_proof",
        "running_missing_submit_proof",
        "attempt_only_upstream",
        "conflicting_upstream",
    ],
)
def test_g73_postgres_active_claim_missing_or_conflicting_submission_identity_is_quarantined(
    postgres_context, scenario,
):
    """An inconsistent active task must not acquire at-most-once submission rights.

    Disposable native PostgreSQL, deterministic FAKE-only; preserve all damaged
    rows and admit the next healthy task without a real Provider request.
    """
    client, sessions, app = postgres_context
    damaged_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        assert damaged is not None and healthy is not None
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        damaged.status = (
            "SUBMITTING" if scenario == "submitting_missing_submit_proof"
            else "INGESTING" if scenario == "conflicting_upstream" else "RUNNING"
        )
        attempt.status = damaged.status
        damaged.created_at = now - datetime.timedelta(minutes=2)
        healthy.created_at = now - datetime.timedelta(minutes=1)
        if scenario == "attempt_only_upstream":
            attempt.submission_started_at = now - datetime.timedelta(minutes=5)
            attempt.upstream_job_id = "legacy-attempt-only-job"
        elif scenario == "conflicting_upstream":
            attempt.submission_started_at = now - datetime.timedelta(minutes=5)
            damaged.upstream_job_id = "legacy-task-job"
            attempt.upstream_job_id = "different-attempt-job"
        assert damaged.lease_owner is None
        assert damaged.lease_epoch == 0
        db.commit()

    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-inconsistent-submit-identity-fence",
            },
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_id
    assert claimed.json()["submissionConsumed"] is False

    with sessions() as db:
        damaged = db.get(DBGenerationTask, damaged_id)
        healthy = db.get(DBGenerationTask, healthy_id)
        attempt = db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == damaged_id,
            DBGenerationAttempt.attempt_no == damaged.attempts,
        )).scalar_one()
        assert damaged.status == attempt.status
        assert damaged.lease_owner is None
        assert damaged.lease_expires_at is None
        assert damaged.lease_epoch == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == damaged_id,
            DBGenerationEvent.event_type.in_(["TASK_CLAIMED", "LEASE_RECOVERED"]),
        )).scalar_one() == 0
        assert healthy.status == "SUBMITTING"
        assert healthy.lease_owner == "g73-inconsistent-submit-identity-fence"
        if scenario == "attempt_only_upstream":
            assert attempt.upstream_job_id == "legacy-attempt-only-job"
        elif scenario == "conflicting_upstream":
            assert damaged.upstream_job_id == "legacy-task-job"
            assert attempt.upstream_job_id == "different-attempt-job"
    app.state.moneyprinter.assert_not_called()


def test_g73_postgres_nested_talking_head_asset_id_poison_does_not_starve_claim(postgres_context):
    """Native disposable PostgreSQL must never bind a list-valued image ID."""
    client, sessions, app = postgres_context
    poisoned_id = _create_task(client)
    healthy_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)

    with sessions() as db:
        bad = db.get(DBGenerationTask, poisoned_id)
        good = db.get(DBGenerationTask, healthy_id)
        assert bad is not None and good is not None
        bad.provider = "deterministic-fake-talking-head"
        bad.request_json = {
            "fakeTaskType": "talking-head",
            "imageAssetId": ["legacy", "bad-nested-asset-identifier"],
            "sourceAssetSha256": "0" * 64,
        }
        bad.created_at = now - datetime.timedelta(minutes=2)
        good.created_at = now - datetime.timedelta(minutes=1)
        db.commit()

    with TestClient(app) as worker_client:
        result = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "g73-postgres-nested-safe-worker",
            },
        )
    assert result.status_code == 200, result.text
    assert result.json()["taskId"] == healthy_id

    with sessions() as db:
        bad = db.get(DBGenerationTask, poisoned_id)
        good = db.get(DBGenerationTask, healthy_id)
        assert bad.status == "QUEUED"
        assert bad.lease_owner is None and bad.lease_epoch == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == poisoned_id,
            DBGenerationEvent.event_type == "TASK_CLAIMED",
        )).scalar_one() == 0
        assert good.status == "SUBMITTING"
        assert good.lease_owner == "g73-postgres-nested-safe-worker"
    app.state.moneyprinter.assert_not_called()


def test_g75_postgres_ineligible_foreign_tenant_stale_fake_talking_head_cannot_transition_circuit(
    postgres_context,
):
    """PostgreSQL: reject a foreign candidate before it may mutate that tenant's OPEN circuit.

    The older foreign talking-head row carries a malformed, non-scalar imageAssetId.
    A different tenant's valid FAKE task must still claim without committing a
    foreign OPEN -> HALF_OPEN transition or issuing a false circuit event.
    This uses a disposable PostgreSQL database and never calls a Provider.
    """
    client, sessions, app = postgres_context
    healthy_task_id = _create_task(client)
    now = datetime.datetime.now(datetime.timezone.utc)
    foreign_tenant_id = "g75-pg-ineligible-foreign"
    foreign_task_id = str(uuid.uuid4())
    old_at = now - datetime.timedelta(hours=2)
    with sessions() as db:
        healthy = db.get(DBGenerationTask, healthy_task_id)
        assert healthy is not None
        assert healthy.capability_snapshot_json["mode"] == "deterministic-fake"

        tenant = DBTenant(
            id=foreign_tenant_id, name="G75 PostgreSQL ineligible foreign",
            slug=foreign_tenant_id, project_quota=50,
            storage_quota_bytes=1024**3, used_storage_bytes=0,
            concurrent_render_quota=2, monthly_render_seconds_quota=3600,
            render_seconds_used=0, quota_period=now.strftime("%Y-%m"),
            created_at=now, updated_at=now,
        )
        db.add(tenant)
        db.flush()
        user = DBUser(
            id="g75-pg-ineligible-owner", tenant_id=foreign_tenant_id,
            email="g75-pg-ineligible-owner@example.com",
            display_name="G75 PostgreSQL foreign owner",
            password_hash="synthetic-fixture-never-used-to-login",
            role="owner", is_active=True, created_at=now, updated_at=now,
        )
        db.add(user)
        db.flush()
        project = DBProject(
            id="g75-pg-ineligible-project", tenant_id=foreign_tenant_id,
            owner_id=user.id, name="G75 stale FAKE talking head",
            timeline={"version": "1.1", "output": None, "tracks": []},
            materials=[], revision=1, created_at=now, updated_at=now,
        )
        db.add(project)
        db.flush()
        stale_request = dict(healthy.request_json)
        stale_request.update({
            "fakeTaskType": "talking-head",
            "imageAssetId": ["not-a-scalar-id"],
            "sourceAssetSha256": "0" * 64,
        })
        stale = DBGenerationTask(
            id=foreign_task_id, tenant_id=foreign_tenant_id,
            project_id=project.id, requested_by=user.id,
            provider="deterministic-fake-talking-head", status="QUEUED",
            progress=0, message="G75 malformed foreign candidate",
            request_json=stale_request, request_hash=healthy.request_hash,
            capability_snapshot_json=dict(healthy.capability_snapshot_json),
            capability_snapshot_hash=healthy.capability_snapshot_hash,
            idempotency_key=str(uuid.uuid4()), attempts=1, max_attempts=1,
            lease_owner=None, lease_epoch=0, circuit_probe_epoch=None,
            lease_expires_at=None, cancel_requested_at=None,
            error_code=None, error_message=None,
            created_at=old_at, updated_at=old_at,
        )
        db.add(stale)
        db.flush()
        db.add(DBGenerationAttempt(
            id=str(uuid.uuid4()), generation_task_id=foreign_task_id,
            attempt_no=1, status="QUEUED", submission_started_at=None,
            upstream_job_id=None, reconciliation_state="NOT_SUBMITTED",
            error_code=None, error_message=None,
            created_at=old_at, completed_at=None,
        ))
        db.add(DBGenerationCircuitState(
            id=str(uuid.uuid4()), tenant_id=foreign_tenant_id,
            provider="moneyprinter", state="OPEN",
            failure_timestamps_json=[old_at.isoformat()],
            opened_at=old_at,
            cooldown_until=now - datetime.timedelta(seconds=5),
            half_open_task_id=None, probe_epoch=11,
            half_open_lease_epoch=None, half_open_issuer=None,
            disabled_reason_code=None, updated_at=old_at,
        ))
        db.commit()

    worker_id = "g75-pg-cross-tenant-fake-worker"
    with TestClient(app) as worker_client:
        claimed = worker_client.post(
            "/internal/generation-tasks/claim",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
        )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == healthy_task_id

    with sessions() as db:
        foreign = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == foreign_tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        stale_after = db.get(DBGenerationTask, foreign_task_id)
        healthy_after = db.get(DBGenerationTask, healthy_task_id)
        assert foreign.state == "OPEN"
        assert foreign.probe_epoch == 11
        assert foreign.half_open_task_id is None
        assert stale_after.status == "QUEUED"
        assert stale_after.lease_owner is None
        assert stale_after.lease_epoch == 0
        assert healthy_after.status == "SUBMITTING"
        assert healthy_after.lease_owner == worker_id
        assert db.execute(select(func.count(DBGenerationProviderEvent.id)).where(
            DBGenerationProviderEvent.tenant_id == foreign_tenant_id,
            DBGenerationProviderEvent.event_type == "CIRCUIT_HALF_OPENED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("revoke_during_upload", [False, True])
def test_g72_postgres_fake_source_revocation_after_claim_blocks_artifact_without_usage(
    postgres_context, monkeypatch, revoke_during_upload,
):
    """PG FAKE-only: rights revocation after Worker claim must block artifact intake.

    Exercising native HTTP, persisted task/rights/usage, and a disposable
    PostgreSQL 16.6 service does not authorize any real Provider invocation.
    """
    client, sessions, app = postgres_context
    project_reply = client.post(
        "/projects", json={"name": "G72 PG rights revoke after claim"}
    )
    assert project_reply.status_code == 201, project_reply.text
    project = project_reply.json()
    now = datetime.datetime.now(datetime.timezone.utc)
    asset_id = str(uuid.uuid4())
    with sessions() as db:
        project_row = db.get(DBProject, project["id"])
        assert project_row is not None
        db.add(DBAssetVersion(
            id=asset_id, tenant_id=project_row.tenant_id,
            project_id=project_row.id, media_id=f"g72-revocation-{asset_id}",
            version_no=1, sha256="b" * 64, media_type="image",
            content_type="image/png", size_bytes=101,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project_row.owner_id, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project_row.tenant_id,
            project_id=project_row.id, asset_version_id=asset_id,
            status="ALLOWED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="g72-pg-allowed-synthetic",
            captured_by=project_row.owner_id, captured_at=now,
        ))
        db.commit()

    key = str(uuid.uuid4())
    body = {
        "text": "offline rights revocation check",
        "durationSeconds": 1,
        "aspectRatio": "9:16",
        "resolution": "720p",
        "voiceId": "synthetic-seed",
        "imageAssetVersionId": asset_id,
        "idempotencyKey": key,
        "expectedProjectRevision": project["revision"],
        "confirmSyntheticTestOnly": True,
    }
    endpoint = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    created = None
    for _ in range(3):
        cap = client.get("/generation/providers/moneyprinter/capabilities")
        assert cap.status_code == 200, cap.text
        body["capabilitySnapshotHash"] = cap.json()["snapshotHash"]
        preflight = client.post(endpoint + "/validate", json=body)
        if preflight.status_code == 409 and preflight.json().get("detail", {}).get("code") in {
            "CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED",
        }:
            continue
        assert preflight.status_code == 200, preflight.text
        body["capabilitySnapshotHash"] = preflight.json()["capabilitySnapshotHash"]
        created = client.post(endpoint, json=body)
        if created.status_code == 202:
            break
        if created.status_code != 409 or created.json().get("detail", {}).get("code") not in {
            "CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED",
        }:
            break
    assert created is not None and created.status_code == 202, (
        created.text if created is not None else "G72 fake creation was not attempted"
    )
    task_id = created.json()["taskId"]
    worker_id = "g72-pg-revoke-claimed-worker"
    claim = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["taskId"] == task_id
    lease_epoch = claim.json()["leaseEpoch"]
    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": worker_id,
        "X-Worker-Lease-Epoch": str(lease_epoch),
    }
    running = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "RUNNING", "progress": 10,
            "message": "synthetic G72 rights revocation race",
            "upstreamJobId": "g72-synthetic-revoke-upstream",
        },
    )
    assert running.status_code == 200, running.text
    ingesting = client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers=worker_headers,
        json={
            "status": "INGESTING", "progress": 95,
            "message": "synthetic G72 artifact upload",
            "upstreamJobId": "g72-synthetic-revoke-upstream",
            "providerArtifactId": "g72-pg-synthetic-revoked-artifact",
        },
    )
    assert ingesting.status_code == 200, ingesting.text

    with sessions() as db:
        current = db.get(DBGenerationTask, task_id)
        assert current is not None
        tenant_id = current.tenant_id
        state_before = (current.status, current.lease_owner, current.lease_epoch)
        assets_before = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project["id"],
        )).scalar_one()
        used_before = db.get(DBTenant, tenant_id).used_storage_bytes
        reserved_before = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one()
        settled_before = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        requested_by = current.requested_by
        db.commit()

    def revoke_now():
        with sessions() as db:
            db.add(DBRightsSnapshot(
                id=str(uuid.uuid4()), tenant_id=tenant_id,
                project_id=project["id"], asset_version_id=asset_id,
                status="REVOKED", purpose="REMIX", territory="TEST",
                valid_from=None, valid_until=None,
                evidence_ref="g72-pg-revoked-synthetic", captured_by=requested_by,
                captured_at=datetime.datetime.now(datetime.timezone.utc)
                            + datetime.timedelta(seconds=1),
            ))
            db.commit()

    def intake():
        with TestClient(app) as worker_client:
            return worker_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers={
                    "X-Worker-Token": WORKER_TOKEN,
                    "X-Worker-Id": worker_id,
                    "X-Worker-Lease-Epoch": str(lease_epoch),
                },
                data={"providerArtifactId": "g72-pg-synthetic-revoked-artifact"},
                files={"file": ("g72-synthetic.mp4", b"g72-revoked-synthetic-bytes", "video/mp4")},
            )

    if revoke_during_upload:
        original_upload = app.state.video_use.upload_media
        upload_started, permit_upload = Event(), Event()

        def hold_upload(*args, **kwargs):
            upload_started.set()
            assert permit_upload.wait(timeout=12), "timed out waiting for rights revocation"
            return original_upload(*args, **kwargs)

        monkeypatch.setattr(app.state.video_use, "upload_media", hold_upload)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(intake)
            try:
                assert upload_started.wait(timeout=12), "artifact intake never reached the FAKE Sidecar"
                revoke_now()  # Durable revocation commits during the in-flight upload.
            finally:
                permit_upload.set()
            rejected = future.result(timeout=20)
    else:
        revoke_now()
        rejected = intake()

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"

    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        assert (task.status, task.lease_owner, task.lease_epoch) == state_before
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project["id"],
        )).scalar_one() == assets_before == 1
        assert db.get(DBTenant, tenant_id).used_storage_bytes == used_before
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == reserved_before == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == settled_before == 0
    app.state.moneyprinter.assert_not_called()



def test_g72_postgres_remix_expires_during_fake_upload_releases_storage_without_settlement(
    postgres_context, monkeypatch,
):
    """Actual PostgreSQL FAKE-only expiry at the upload/final-commit boundary.

    The source's REMIX evidence is valid when Worker starts intake but expires
    while the test-only media adapter writes bytes. A final rights recheck must
    reject the artifact, undo only this upload's quota hold and never settle.
    No real clock sleeping, Provider call, policy or protected schema change.
    """
    from app import main as api_main

    owner, sessions, app = postgres_context
    worker_id = "g72-expiring-remix-worker"
    before_expiry = datetime.datetime.now(datetime.timezone.utc)
    expires_at = before_expiry + datetime.timedelta(seconds=45)
    task_id, source_id, project_id, tenant_id, requested_by, lease_epoch = (
        _g72_create_claimed_typed_fake_source(
            owner, sessions, worker_id, source_sha="e" * 64,
            valid_until=expires_at,
        )
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        task.status = "RUNNING"
        task.lease_expires_at = before_expiry + datetime.timedelta(hours=2)
        db.commit()
        initial_storage = db.get(DBTenant, tenant_id).used_storage_bytes
        initial_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        initial_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        before_lease = (task.lease_owner, task.lease_epoch, task.status)
    assert initial_assets == 1 and initial_settled == 0

    clock = {"now": before_expiry}
    monkeypatch.setattr(api_main, "utc_now", lambda: clock["now"])
    real_upload = app.state.video_use.upload_media
    uploads = []

    def expire_during_upload(*args, **kwargs):
        uploaded = real_upload(*args, **kwargs)
        uploads.append(uploaded["mediaId"])
        clock["now"] = expires_at + datetime.timedelta(seconds=1)
        return uploaded

    monkeypatch.setattr(app.state.video_use, "upload_media", expire_during_upload)
    rejected = owner.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN,
            "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(lease_epoch),
        },
        data={"providerArtifactId": "g72-synthetic-rights-expired"},
        files={"file": ("synthetic.mp4", b"synthetic-expiry-bytes", "video/mp4")},
    )
    assert uploads, "Test did not reach the fake media upload boundary"
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"

    with sessions() as db:
        after_task = db.get(DBGenerationTask, task_id)
        assert (after_task.lease_owner, after_task.lease_epoch, after_task.status) == before_lease
        assert after_task.asset_version_id is None
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == initial_assets
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == initial_settled
        assert db.get(DBTenant, tenant_id).used_storage_bytes == initial_storage
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("poisoned_request", [
    ["legacy", "corrupt-talking-head-request"],
    {"imageAssetId": ["wrong", "asset-id"], "sourceAssetSha256": "a" * 64},
])
def test_g72_postgres_claimed_legacy_malformed_source_intake_fails_closed(
    postgres_context, poisoned_request,
):
    """FAKE-only: a previously leased, subsequently corrupted request must not 500.

    A legacy or damaged persisted task can be leased on an older build.  The
    intake boundary must reject the row before binding a list/dict as an SQL
    asset ID, without altering bytes, usage, lease, or asset state.  This is
    not a real Provider call or a new authorization policy.
    """
    client, sessions, app = postgres_context
    task_id = _create_task(client)
    worker_id = "g72-pg-legacy-poison-worker"
    claim = client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["taskId"] == task_id
    epoch = claim.json()["leaseEpoch"]

    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        # Simulate a historical leased FAKE talking-head task whose durable
        # request JSON was malformed after its lease was already issued.
        task.provider = "deterministic-fake-talking-head"
        task.request_json = poisoned_request
        task.status = "RUNNING"
        tenant_id = task.tenant_id
        project_id = task.project_id
        db.commit()
        before_state = (task.status, task.lease_owner, task.lease_epoch)
        before_bytes = db.get(DBTenant, tenant_id).used_storage_bytes
        before_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        before_entries = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
        )).scalar_one()
        before_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one()

    rejected = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN,
            "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(epoch),
        },
        data={"providerArtifactId": "g72-poisoned-legacy-intake"},
        files={"file": ("synthetic.mp4", b"synthetic-test-only", "video/mp4")},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "FAKE_TALKING_HEAD_REQUEST_INVALID"

    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        assert (task.status, task.lease_owner, task.lease_epoch) == before_state
        assert db.get(DBTenant, tenant_id).used_storage_bytes == before_bytes
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == before_assets
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
        )).scalar_one() == before_entries
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one() == before_events
    app.state.moneyprinter.assert_not_called()


def test_g72_postgres_revoke_after_final_rights_read_is_serialized(postgres_context, monkeypatch):
    """Probe REMIX rights/worker-commit linearization without test-side deadlock.

    Owner revocation starts after Worker's last ALLOWED read. When a future
    shared transaction lock serializes the endpoints, the Owner POST may wait
    for Worker's commit: do not synchronously wait for Owner before resuming.
    Owner revocation must not ACK while the Worker holds the source asset
    row lock after its last REMIX check. The Worker commits first, then Owner
    revocation may commit; the already-ingested asset stays export-blocked.
    FAKE-only, disposable PostgreSQL; no rights/storage policy modification.
    """
    from app import main as api_main

    owner, sessions, app = postgres_context
    worker_id = "g72-final-read-revocation-probe"
    source_digest = "c" * 64
    task_id, asset_id, project_id, tenant_id, requested_by, epoch = (
        _g72_create_claimed_typed_fake_source(
            owner, sessions, worker_id, source_sha=source_digest,
        )
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        task.status = "RUNNING"
        db.commit()
        base_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        base_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        base_usage = db.get(DBTenant, tenant_id).used_storage_bytes
        db.rollback()
    assert base_assets == 1 and base_settled == 0

    uploaded, final_read, resume = Event(), Event(), Event()
    original_upload = app.state.video_use.upload_media
    original_decision = api_main.rights_decision

    def fake_upload(*args, **kwargs):
        result = original_upload(*args, **kwargs)
        uploaded.set()
        return result

    def hold_after_final_rights_read(snapshot, at):
        decision = original_decision(snapshot, at)
        if (
            uploaded.is_set() and not final_read.is_set()
            and snapshot is not None and snapshot.asset_version_id == asset_id
            and snapshot.purpose == "REMIX" and snapshot.status == "ALLOWED"
        ):
            assert decision["allowed"] is True
            final_read.set()
            assert resume.wait(timeout=20), "final-rights-read probe release timed out"
        return decision

    monkeypatch.setattr(app.state.video_use, "upload_media", fake_upload)
    monkeypatch.setattr(api_main, "rights_decision", hold_after_final_rights_read)

    def worker_intake():
        with TestClient(app) as worker:
            return worker.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers={
                    "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id,
                    "X-Worker-Lease-Epoch": str(epoch),
                },
                data={"providerArtifactId": "g72-final-read-commit-race"},
                files={"file": ("synthetic.mp4", b"fake-only-g72-final-race", "video/mp4")},
            )

    revoke_started, revoke_finished = Event(), Event()

    def owner_revoke():
        revoke_started.set()
        try:
            return owner.post(
                f"/projects/{project_id}/asset-versions/{asset_id}/rights-snapshots",
                json={
                    "status": "REVOKED", "purpose": "REMIX",
                    "territory": "TEST", "evidenceRef": "g72-final-race-owner-revoke",
                },
            )
        finally:
            revoke_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(worker_intake)
        revoke_future = None
        revoke_committed_before_worker_resume = False
        try:
            assert final_read.wait(timeout=15), (
                "Worker did not reach final REMIX decision after FAKE upload"
            )
            revoke_future = pool.submit(owner_revoke)
            assert revoke_started.wait(timeout=5), "Owner revocation never started"
            # Bounded observation: a correctly serialized Owner writer can
            # block on Worker's source lock; never wait indefinitely here.
            revoke_committed_before_worker_resume = revoke_finished.wait(timeout=3)
        finally:
            resume.set()
        response = future.result(timeout=25)
        assert revoke_future is not None
        revoke = revoke_future.result(timeout=25)
        assert revoke.status_code == 201, revoke.text

    with sessions() as db:
        latest = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == asset_id,
            DBRightsSnapshot.purpose == "REMIX",
        ).order_by(DBRightsSnapshot.captured_at.desc()).limit(1)).scalar_one()
        asset_count = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        settled_count = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        usage_after = db.get(DBTenant, tenant_id).used_storage_bytes
        task_after = db.get(DBGenerationTask, task_id)
        task_state = task_after.status
    assert latest.status == "REVOKED", "Owner revocation did not persist"
    assert not revoke_committed_before_worker_resume, (
        "G72 revoke was acknowledged while Worker held the post-REMIX source "
        "lock; the rights check and final asset+SETTLED commit are not serialized"
    )
    # The Worker acquired the source lock before the Owner POST. It may ingest
    # and settle first; the subsequent revoked source remains non-exportable.
    assert response.status_code == 201, response.text
    assert asset_count == 2 and settled_count == 1
    assert usage_after >= base_usage
    assert task_state == "RIGHTS_BLOCKED"
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("revocation", ["inactive", "downgraded", "project_reassigned"])
def test_g72_postgres_owner_revoked_during_fake_upload_is_fenced_before_commit(
    postgres_context, monkeypatch, revocation,
):
    """A pre-upload Owner check cannot authorize post-upload asset settlement.

    Actual disposable PostgreSQL, typed HTTP FAKE task and Sidecar stub.
    Revocation commits after the first quota reservation, while upload is
    in flight. No new asset/SETTLED or permanent quota usage may survive.
    """
    owner, sessions, app = postgres_context
    worker_id = "g72-final-originator-revoke-" + revocation
    task_id, _source_id, project_id, tenant_id, requested_by, epoch = (
        _g72_create_claimed_typed_fake_source(
            owner, sessions, worker_id, source_sha="d" * 64,
        )
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        task.status = "RUNNING"
        db.commit()
        initial_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        initial_bytes = db.get(DBTenant, tenant_id).used_storage_bytes

    original_upload = app.state.video_use.upload_media
    uploaded = Event()
    sidecar_upload_attempts = []

    def revoke_while_uploading(*args, **kwargs):
        sidecar_upload_attempts.append(1)
        result = original_upload(*args, **kwargs)
        with sessions() as db:
            original = db.get(DBUser, requested_by)
            if revocation == "inactive":
                original.is_active = False
            elif revocation == "downgraded":
                original.role = "editor"
            else:
                now = datetime.datetime.now(datetime.timezone.utc)
                replacement = DBUser(
                    id=str(uuid.uuid4()), tenant_id=tenant_id,
                    email=f"g72-pg-new-owner-{uuid.uuid4()}@example.com",
                    display_name="FAKE replacement Owner",
                    password_hash="unused-fake-test-hash",
                    role="owner", is_active=True,
                    created_at=now, updated_at=now,
                )
                db.add(replacement)
                db.flush()
                db.get(DBProject, project_id).owner_id = replacement.id
            db.commit()
        uploaded.set()
        return result

    monkeypatch.setattr(app.state.video_use, "upload_media", revoke_while_uploading)
    result = owner.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(epoch),
        },
        data={"providerArtifactId": "g72-final-owner-revoke-" + revocation},
        files={"file": ("synthetic.mp4", b"fake-only-final-owner-revoke", "video/mp4")},
    )
    assert uploaded.is_set(), "Test must reach Sidecar after committed reservation"
    assert len(sidecar_upload_attempts) == 1
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task.asset_version_id is None
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == initial_assets
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.get(DBTenant, tenant_id).used_storage_bytes == initial_bytes
        # Existing quota debit reversal must be paired with one immutable
        # release receipt bound to the exact OPENED event (FAKE-only).
        opened = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
        )).scalars().all()
        released = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalars().all()
        assert len(opened) == len(released) == 1
        opened_meta = opened[0].metadata_json
        released_meta = released[0].metadata_json
        assert released_meta["storageReservationEventId"] == opened[0].id
        assert released_meta["reasonCode"] == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
        assert released[0].actor_type == "SYSTEM"
        assert released[0].actor_id == "api"
        for field in (
            "workerId", "leaseEpoch", "providerArtifactId",
            "mediaId", "sha256", "reservedStorageBytes",
        ):
            assert released_meta[field] == opened_meta[field]
        # The Sidecar already accepted bytes before final authorization failed.
        # A successful HTTP 409 is insufficient: the orphan must be durably
        # discoverable for Owner-only reconciliation, not silently forgotten.
        failures = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalars().all()
        assert len(failures) == 1
        orphan = failures[0].metadata_json
        assert isinstance(orphan, dict)
        assert orphan["reasonCode"] == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
        assert orphan["orphanedMediaId"] == _expected_media_id(
            project_id, f"generation:{task_id}"
        )
        assert orphan["orphanedMediaSha256"] == hashlib.sha256(
            b"fake-only-final-owner-revoke"
        ).hexdigest()
        assert orphan["orphanedMediaSizeBytes"] == len(
            b"fake-only-final-owner-revoke"
        )
        assert orphan["originalProviderArtifactId"] == (
            "g72-final-owner-revoke-" + revocation
        )

    # A Worker retry after revocation must not re-upload this orphan or
    # recreate quota; existing Owner/tenant/project fences remain in force.
    repeated = owner.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(epoch),
        },
        data={"providerArtifactId": "g72-final-owner-revoke-" + revocation},
        files={"file": ("synthetic.mp4", b"fake-only-final-owner-revoke", "video/mp4")},
    )
    assert repeated.status_code == 409, repeated.text
    assert repeated.json()["detail"]["code"] == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
    assert len(sidecar_upload_attempts) == 1
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalar_one() == 1
        assert db.get(DBTenant, tenant_id).used_storage_bytes == initial_bytes
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one() == 1
    # A later reactivation/reassignment is not a durable orphan-recovery ACK.
    # The now-paired storage release must not accidentally allow a second
    # Sidecar upload or settlement of the previously revoked artifact.
    with sessions() as db:
        original = db.get(DBUser, requested_by)
        original.is_active = True
        original.role = "owner"
        db.get(DBProject, project_id).owner_id = requested_by
        db.commit()
    restored_retry = owner.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(epoch),
        },
        data={"providerArtifactId": "g72-final-owner-revoke-" + revocation},
        files={"file": ("synthetic.mp4", b"fake-only-final-owner-revoke", "video/mp4")},
    )
    assert restored_retry.status_code == 409, restored_retry.text
    assert restored_retry.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
    assert len(sidecar_upload_attempts) == 1
    with sessions() as db:
        assert db.get(DBTenant, tenant_id).used_storage_bytes == initial_bytes
        assert db.get(DBGenerationTask, task_id).asset_version_id is None
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()


def test_g72_postgres_originator_disable_after_final_lock_waits_for_commit(
    postgres_context, monkeypatch,
):
    """Owner deactivation must not ACK inside Worker's final commit window.

    Hook the completed PostgreSQL SELECT FOR UPDATE on the original Owner.
    Once the Worker owns the row lock, a separate disable transaction must
    wait rather than racing past the final asset/SETTLED write.
    """
    owner, sessions, app = postgres_context
    worker_id = "g72-owner-final-auth-rowlock-worker"
    task_id, _source, project_id, tenant_id, requested_by, epoch = (
        _g72_create_claimed_typed_fake_source(
            owner, sessions, worker_id, source_sha="e" * 64,
        )
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        task.status = "RUNNING"
        db.commit()
        initial_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()

    final_owner_locked, resume_worker = Event(), Event()
    disable_started, disable_finished = Event(), Event()
    engine = sessions.kw["bind"]

    def after_final_owner_lock(conn, cursor, statement, parameters, context, executemany):
        normalized = " ".join(statement.lower().split())
        if (
            "select" in normalized and "users.id" in normalized
            and "for update" in normalized and not final_owner_locked.is_set()
        ):
            final_owner_locked.set()
            assert resume_worker.wait(timeout=20), "Final Owner rowlock test timed out"

    def worker_intake():
        with TestClient(app) as client:
            return client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers={
                    "X-Worker-Token": WORKER_TOKEN,
                    "X-Worker-Id": worker_id,
                    "X-Worker-Lease-Epoch": str(epoch),
                },
                data={"providerArtifactId": "g72-final-owner-rowlock"},
                files={"file": ("synthetic.mp4", b"fake-only-owner-commit-race", "video/mp4")},
            )

    def disable_owner():
        disable_started.set()
        try:
            with sessions() as db:
                original = db.get(DBUser, requested_by)
                original.is_active = False
                db.commit()
            return True
        finally:
            disable_finished.set()

    event.listen(engine, "after_cursor_execute", after_final_owner_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker_future = pool.submit(worker_intake)
            disable_future = None
            observed_lock_wait = False
            try:
                assert final_owner_locked.wait(timeout=15), (
                    "Worker never acquired original Owner final row lock"
                )
                disable_future = pool.submit(disable_owner)
                assert disable_started.wait(timeout=5)
                observed_lock_wait = _wait_until_postgres_reports_lock_wait(
                    sessions, "users", timeout_seconds=6,
                )
                assert not disable_finished.is_set(), (
                    "Owner deactivation committed while Worker held final row lock"
                )
            finally:
                resume_worker.set()
            worker_result = worker_future.result(timeout=25)
            assert disable_future is not None
            assert disable_future.result(timeout=25)
        assert observed_lock_wait, "PostgreSQL did not show a blocked Owner UPDATE"
        assert worker_result.status_code == 201, worker_result.text
        with sessions() as db:
            assert db.get(DBUser, requested_by).is_active is False
            task = db.get(DBGenerationTask, task_id)
            assert task.asset_version_id is not None
            assert db.execute(select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == project_id,
            )).scalar_one() == initial_assets + 1
            assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
                DBGenerationUsageEntry.task_id == task_id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )).scalar_one() == 1
        app.state.moneyprinter.assert_not_called()
    finally:
        resume_worker.set()
        event.remove(engine, "after_cursor_execute", after_final_owner_lock)


def test_g70_g71_postgres_bounded_liveness_pool_exhaustion_is_retryable(postgres_context):
    """A full native PostgreSQL pool must fail closed before quota or FAKE Sidecar.

    The intake DB session holds the sole connection. Its dedicated advisory
    liveness checkout must not leak an HTTP 500, quota reservation, or asset.
    The small pool is isolated from the main disposable PostgreSQL fixture.
    """
    from fastapi import HTTPException
    from sqlalchemy import create_engine

    owner_client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(owner_client)
    payload = b"g70-g71-bounded-pg-pool-fake-only"
    with sessions() as db:
        task_before = db.get(DBGenerationTask, task_id)
        assert task_before is not None
        tenant_id = task_before.tenant_id
        usage_before = db.get(DBTenant, tenant_id).used_storage_bytes

    limited_engine = create_engine(
        os.environ["AETHER_TEST_POSTGRES_URL"],
        pool_size=1, max_overflow=0, pool_timeout=0.2,
    )
    limited_sessions = sessionmaker(autocommit=False, autoflush=False, bind=limited_engine)

    def limited_dependency():
        with limited_sessions() as db:
            yield db

    sidecar = MagicMock(wraps=FakeMediaStore())
    limited_app = create_app(
        app_engine=limited_engine,
        db_dependency=limited_dependency,
        video_use_adapter=sidecar,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="g76-postgres-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    route = next(
        route for route in limited_app.routes
        if getattr(route, "path", None) == "/internal/generation-tasks/{task_id}/artifact-intake"
    )
    try:
        with limited_sessions() as db:
            with pytest.raises(HTTPException) as failed:
                route.endpoint(
                    task_id,
                    providerArtifactId=artifact_id,
                    file=UploadFile(
                        file=BytesIO(payload),
                        filename="synthetic-g70-pool.mp4",
                        headers=Headers({"content-type": "video/mp4"}),
                    ),
                    x_worker_token=WORKER_TOKEN,
                    x_worker_id=headers["X-Worker-Id"],
                    x_worker_lease_epoch=int(headers["X-Worker-Lease-Epoch"]),
                    db=db,
                )
            assert failed.value.status_code == 503
            assert failed.value.detail["code"] == "STORAGE_HOLD_DB_CAPACITY_UNAVAILABLE"
            db.rollback()
    finally:
        limited_engine.dispose()

    with sessions() as db:
        task_after = db.get(DBGenerationTask, task_id)
        assert task_after is not None
        assert task_after.status == "INGESTING"
        assert task_after.asset_version_id is None
        assert db.get(DBTenant, tenant_id).used_storage_bytes == usage_before
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
        )).scalar_one() == 0
    sidecar.upload_media.assert_not_called()
    app.state.moneyprinter.assert_not_called()



def test_g70_g71_postgres_two_connection_pool_concurrent_retry_recovers_after_release(
    postgres_context,
):
    """Native bounded-pool FAKE-only fault: reject pressure, then release safely.

    The first request holds the durable liveness connection while its fake
    Sidecar upload is paused. A second request holds the remaining pool slot,
    so its separate liveness checkout must return bounded HTTP 503 instead of
    a 500, duplicated quota, or Sidecar call. After the first completes, all
    pooled connections must be returned and identical terminal replay must
    remain idempotent.
    """
    from fastapi import HTTPException
    from sqlalchemy import create_engine

    owner_client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-two-connection-pool-fake-only"
    with sessions() as db:
        task_before = db.get(DBGenerationTask, task_id)
        assert task_before is not None
        tenant_id, project_id = task_before.tenant_id, task_before.project_id
        usage_before = db.get(DBTenant, tenant_id).used_storage_bytes

    limited_engine = create_engine(
        os.environ["AETHER_TEST_POSTGRES_URL"],
        pool_size=2, max_overflow=0, pool_timeout=0.3,
    )
    limited_sessions = sessionmaker(
        autocommit=False, autoflush=False, bind=limited_engine,
    )

    def limited_dependency():
        with limited_sessions() as db:
            yield db

    fake_store = FakeMediaStore()
    sidecar = MagicMock(wraps=fake_store)
    upload_entered, upload_resume = Event(), Event()

    def paused_fake_upload(*args, **kwargs):
        upload_entered.set()
        assert upload_resume.wait(timeout=15), "fake Sidecar was not resumed"
        return fake_store.upload_media(*args, **kwargs)

    sidecar.upload_media.side_effect = paused_fake_upload
    limited_app = create_app(
        app_engine=limited_engine,
        db_dependency=limited_dependency,
        video_use_adapter=sidecar,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="g76-postgres-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    route = next(
        route for route in limited_app.routes
        if getattr(route, "path", None)
        == "/internal/generation-tasks/{task_id}/artifact-intake"
    )

    def intake_direct():
        with limited_sessions() as db:
            return route.endpoint(
                task_id,
                providerArtifactId=artifact_id,
                file=UploadFile(
                    file=BytesIO(payload),
                    filename="synthetic-g70-pool-concurrency.mp4",
                    headers=Headers({"content-type": "video/mp4"}),
                ),
                x_worker_token=WORKER_TOKEN,
                x_worker_id=headers["X-Worker-Id"],
                x_worker_lease_epoch=int(headers["X-Worker-Lease-Epoch"]),
                db=db,
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(intake_direct)
            try:
                assert upload_entered.wait(timeout=12), (
                    "first Worker did not enter FAKE Sidecar upload"
                )
                # During one long Sidecar write, repeated bounded-pool
                # retries must remain fail-closed and release their own DB
                # connections; a single successful 503 is insufficient proof.
                started = time.monotonic()
                for _retry in range(8):
                    with pytest.raises(HTTPException) as blocked:
                        intake_direct()
                    assert blocked.value.status_code == 503
                    assert (
                        blocked.value.detail["code"]
                        == "STORAGE_HOLD_DB_CAPACITY_UNAVAILABLE"
                    )
                    # Liveness from the first request is still checked out,
                    # but retry sessions must not accumulate pool checkouts.
                    assert limited_engine.pool.checkedout() == 1
                    sidecar.upload_media.assert_called_once()
                assert time.monotonic() - started < 12, (
                    "eight capacity failures must remain bounded"
                )
            finally:
                upload_resume.set()
            completed = first_future.result(timeout=20)

        assert completed["results"][0]["assetVersionId"]
        assert limited_engine.pool.checkedout() == 0, (
            "request, liveness and probe sessions must release all connections"
        )
        replay = intake_direct()
        assert replay["results"][0]["assetVersionId"] == (
            completed["results"][0]["assetVersionId"]
        )
        assert limited_engine.pool.checkedout() == 0
        sidecar.upload_media.assert_called_once()
    finally:
        upload_resume.set()
        limited_engine.dispose()

    with sessions() as db:
        task_after = db.get(DBGenerationTask, task_id)
        assert task_after is not None and task_after.asset_version_id is not None
        assert db.get(DBTenant, tenant_id).used_storage_bytes == (
            usage_before + len(payload)
        )
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_initial_pool_checkout_exhaustion_is_bounded_and_nonmutating(
    postgres_context,
):
    """FAKE-only native HTTP proof: initial DB checkout exhaustion must be 503.

    Existing G70/G71 regression covers the *second* liveness connection.
    Multiple busy Workers can also consume every slot before the request
    Session's first SELECT. An exception here must not become HTTP 500, nor
    create a storage hold, asset or Sidecar upload. This deliberately uses
    a disposable PostgreSQL pool and no real Provider.
    """
    from sqlalchemy import create_engine

    client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        client,
    )
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id, project_id = task.tenant_id, task.project_id
        bytes_before = db.get(DBTenant, tenant_id).used_storage_bytes
        assets_before = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one()
        events_before = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one()

    limited_engine = create_engine(
        os.environ["AETHER_TEST_POSTGRES_URL"],
        pool_size=2, max_overflow=0, pool_timeout=0.2,
    )
    limited_sessions = sessionmaker(
        autocommit=False, autoflush=False, bind=limited_engine,
    )

    def limited_dependency():
        with limited_sessions() as db:
            yield db

    sidecar = MagicMock(wraps=FakeMediaStore())
    limited_app = create_app(
        app_engine=limited_engine,
        db_dependency=limited_dependency,
        video_use_adapter=sidecar,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="g76-postgres-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    # Do not run app startup while deliberately pinning all DB slots; the
    # existing fixture has already created the database and authenticated task.
    request_client = TestClient(limited_app, raise_server_exceptions=False)
    try:
        with limited_engine.connect() as first_slot, limited_engine.connect() as second_slot:
            assert limited_engine.pool.checkedout() == 2
            started = time.monotonic()
            for _retry in range(3):
                rejected = request_client.post(
                    f"/internal/generation-tasks/{task_id}/artifact-intake",
                    headers=headers,
                    data={"providerArtifactId": artifact_id},
                    files={"file": ("synthetic.mp4", b"pool-exhaustion-fake", "video/mp4")},
                )
                assert rejected.status_code == 503, rejected.text
                assert rejected.json()["detail"]["code"] == "DATABASE_CAPACITY_UNAVAILABLE"
                assert limited_engine.pool.checkedout() == 2
            assert time.monotonic() - started < 5, "pool exhaustion must be bounded"
        assert limited_engine.pool.checkedout() == 0
        sidecar.upload_media.assert_not_called()
    finally:
        request_client.close()
        limited_engine.dispose()

    with sessions() as db:
        assert db.get(DBTenant, tenant_id).used_storage_bytes == bytes_before
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == assets_before
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one() == events_before
    app.state.moneyprinter.assert_not_called()

def test_g70_g71_postgres_contended_liveness_advisory_lock_is_bounded_and_nonmutating(
    postgres_context, monkeypatch,
):
    """FAKE-only HTTP regression: a held PG advisory key cannot hang intake.

    Force only the API reservation UUID to collide with a lock held by an
    independent PostgreSQL connection. The request must return bounded 503
    without quota, asset, audit or Sidecar writes; once the lock is released,
    the same task must be ingested normally with exactly one committed asset.
    """
    import app.main as main_module

    client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        client,
    )
    payload = b"g70-g71-advisory-contention-fake-only"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id, project_id = task.tenant_id, task.project_id
        before_bytes = db.get(DBTenant, tenant_id).used_storage_bytes
        before_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one()

    forced_reservation_id = uuid.UUID("00000000-0000-4000-8000-00000000a71f")
    forced_lock_key = int(forced_reservation_id) & 0x7FFFFFFFFFFFFFFF
    real_uuid_module = main_module.uuid

    class ScopedReservationUUID:
        def __getattr__(self, name):
            return getattr(real_uuid_module, name)

        def uuid4(self):
            return forced_reservation_id

    real_upload = FakeMediaStore.upload_media
    attempted_upload = MagicMock(
        side_effect=AssertionError("held advisory lock must not reach Sidecar"),
    )
    engine = sessions.kw["bind"]
    with engine.connect() as blocker:
        blocker.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": forced_lock_key},
        )
        blocker.commit()
        try:
            monkeypatch.setattr(main_module, "uuid", ScopedReservationUUID())
            monkeypatch.setattr(FakeMediaStore, "upload_media", attempted_upload)
            started = time.monotonic()
            for _retry in range(3):
                rejected = client.post(
                    f"/internal/generation-tasks/{task_id}/artifact-intake",
                    headers=headers,
                    data={"providerArtifactId": artifact_id},
                    files={"file": ("synthetic.mp4", payload, "video/mp4")},
                )
                assert rejected.status_code == 503, rejected.text
                assert rejected.json()["detail"]["code"] == "STORAGE_HOLD_LOCK_CONTENDED"
            assert time.monotonic() - started < 5, "advisory contention must be bounded"
            attempted_upload.assert_not_called()
            with sessions() as db:
                assert db.get(DBTenant, tenant_id).used_storage_bytes == before_bytes
                assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                    DBGenerationEvent.generation_task_id == task_id,
                )).scalar_one() == before_events
                assert db.execute(select(func.count(DBAssetVersion.id)).where(
                    DBAssetVersion.project_id == project_id,
                )).scalar_one() == 0
        finally:
            monkeypatch.setattr(main_module, "uuid", real_uuid_module)
            monkeypatch.setattr(FakeMediaStore, "upload_media", real_upload)
            unlocked = blocker.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": forced_lock_key},
            ).scalar_one()
            blocker.commit()
            assert unlocked is True

    accepted = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=headers,
        data={"providerArtifactId": artifact_id},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert accepted.status_code == 201, accepted.text
    replay = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=headers,
        data={"providerArtifactId": artifact_id},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert replay.status_code == 201, replay.text
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "RIGHTS_BLOCKED"
        assert task.asset_version_id is not None
        assert db.get(DBTenant, tenant_id).used_storage_bytes == before_bytes + len(payload)
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == 1
        events = list(db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalars())
        assert sum(event.event_type == "STORAGE_RESERVATION_OPENED" for event in events) == 1
        # Successful first-time intake has no compensating RELEASED event.
        # The older duplicate-ACK helper expects one and does not apply here.
        assert sum(event.event_type == "STORAGE_RESERVATION_RELEASED" for event in events) == 0
        assert sum(event.event_type == "ARTIFACT_INGESTED" for event in events) == 1
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_live_independent_process_duplicate_intake_is_idempotent(
    postgres_context,
):
    """Live two-process API intake against shared PG must not double-account.

    The earlier concurrent HTTP test uses threads within one API process;
    the SIGKILL test runs the second API only after the first one is dead.
    Here an independent API process holds an actual committed storage
    reservation and PostgreSQL liveness lock during its FAKE Sidecar upload.
    A different API instance completes the same artifact while it is alive.
    This checks cross-process quota/asset/release receipts, not production
    sizing, a real Sidecar, Provider billing or crash-durable reconciliation.
    """
    import json
    import subprocess
    import sys
    import tempfile

    if sys.platform != "linux":
        pytest.skip("independent API process proof uses disposable Linux CI")

    owner_client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(
        owner_client
    )
    payload = b"g70-g71-live-two-process-fake-only-shared-postgres"
    child_script = r'''
import json
import os
import time
from io import BytesIO
from pathlib import Path
from fastapi import UploadFile
from starlette.datastructures import Headers
from sqlalchemy.orm import sessionmaker
from app.database import build_engine
from app.main import create_app
from test_g76_postgres_concurrency import FakeMediaStore

class PausedFakeSidecar(FakeMediaStore):
    def upload_media(self, project_id, filename, content_type, stream, *,
                     idempotency_key=None, content_sha256=None):
        result = super().upload_media(
            project_id, filename, content_type, stream,
            idempotency_key=idempotency_key, content_sha256=content_sha256,
        )
        Path(os.environ["AETHER_TEST_LIVE_CHILD_READY"]).write_text("FAKE_UPLOAD_HELD")
        deadline = time.monotonic() + 20
        while not Path(os.environ["AETHER_TEST_LIVE_CHILD_RELEASE"]).exists():
            if time.monotonic() > deadline:
                raise RuntimeError("independent FAKE Sidecar release timed out")
            time.sleep(0.05)
        return result

engine = build_engine(os.environ["AETHER_TEST_POSTGRES_URL"])
sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
def get_db():
    with sessions() as db:
        yield db
app = create_app(
    app_engine=engine,
    db_dependency=get_db,
    video_use_adapter=PausedFakeSidecar(),
    bootstrap_admin_password=os.environ["AETHER_TEST_LIVE_CHILD_OWNER_PASSWORD"],
    bootstrap_admin_email="g76-postgres-owner@example.com",
    worker_token=os.environ["AETHER_TEST_LIVE_CHILD_WORKER_TOKEN"],
    cookie_secure=False,
    enforce_csrf=True,
    generation_provider_mode="deterministic-fake",
)
route = next(
    r for r in app.routes
    if getattr(r, "path", None) == "/internal/generation-tasks/{task_id}/artifact-intake"
)
with sessions() as db:
    result = route.endpoint(
        os.environ["AETHER_TEST_LIVE_CHILD_TASK"],
        providerArtifactId=os.environ["AETHER_TEST_LIVE_CHILD_ARTIFACT"],
        file=UploadFile(
            file=BytesIO(b"g70-g71-live-two-process-fake-only-shared-postgres"),
            filename="synthetic-g70-g71-live.mp4",
            headers=Headers({"content-type": "video/mp4"}),
        ),
        x_worker_token=os.environ["AETHER_TEST_LIVE_CHILD_WORKER_TOKEN"],
        x_worker_id=os.environ["AETHER_TEST_LIVE_CHILD_WORKER"],
        x_worker_lease_epoch=int(os.environ["AETHER_TEST_LIVE_CHILD_EPOCH"]),
        db=db,
    )
Path(os.environ["AETHER_TEST_LIVE_CHILD_RESULT"]).write_text(
    json.dumps(result["results"][0], sort_keys=True)
)
engine.dispose()
'''
    with tempfile.TemporaryDirectory(prefix="aether-g70-live-multiprocess-") as tmp:
        ready = Path(tmp, "ready")
        release = Path(tmp, "release")
        result_path = Path(tmp, "result.json")
        child_env = os.environ.copy()
        child_env.update({
            "AETHER_TEST_LIVE_CHILD_READY": str(ready),
            "AETHER_TEST_LIVE_CHILD_RELEASE": str(release),
            "AETHER_TEST_LIVE_CHILD_RESULT": str(result_path),
            "AETHER_TEST_LIVE_CHILD_TASK": task_id,
            "AETHER_TEST_LIVE_CHILD_ARTIFACT": artifact_id,
            "AETHER_TEST_LIVE_CHILD_WORKER": headers["X-Worker-Id"],
            "AETHER_TEST_LIVE_CHILD_EPOCH": headers["X-Worker-Lease-Epoch"],
            "AETHER_TEST_LIVE_CHILD_WORKER_TOKEN": WORKER_TOKEN,
            "AETHER_TEST_LIVE_CHILD_OWNER_PASSWORD": OWNER_PASSWORD,
        })
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_script],
            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        parent_response = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                _, err = child.communicate(timeout=5) if child.poll() is not None else ("", "")
                pytest.fail(f"independent API did not reach FAKE Sidecar: {err[-1200:]}")
            assert child.poll() is None, "independent API exited before live overlap"

            # This independent main-process HTTP client races the live child
            # while its first hold and session-level PG lock are still active.
            parent_response = _post_g70_g71_duplicate_intake(
                app, task_id, headers, artifact_id, payload
            )
            assert parent_response.status_code == 201, parent_response.text
            assert child.poll() is None, "child must still be in FAKE upload"
        finally:
            release.write_text("release")
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
            _, child_err = child.communicate(timeout=5)

        assert child.returncode == 0, child_err[-2000:]
        assert result_path.exists(), child_err[-1200:]
        child_result = json.loads(result_path.read_text())
        assert child_result["assetVersionId"] == (
            parent_response.json()["results"][0]["assetVersionId"]
        )
        assert child_result["mediaId"] == parent_response.json()["results"][0]["mediaId"]

    _assert_g70_g71_postgres_single_ingest(sessions, task_id, payload)
    with sessions() as db:
        holds = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
        )).scalars().all()
        assert len(holds) == 2, "both live API processes reserved before Sidecar"
        releases = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
        )).scalars().all()
        assert len(releases) == 1, "only the duplicate reservation was released"
        liveness_keys = [hold.metadata_json["livenessLockKey"] for hold in holds]

    # Independent backend must reacquire every prior session-level lock:
    # process exit or normal request completion cannot strand a pooled lock.
    from sqlalchemy import create_engine
    check_engine = create_engine(os.environ["AETHER_TEST_POSTGRES_URL"])
    try:
        with check_engine.connect() as conn:
            for key in liveness_keys:
                assert conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar_one() is True
                assert conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                ).scalar_one() is True
            conn.rollback()
    finally:
        check_engine.dispose()
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("underfunded", [False, True])
def test_g70_g71_postgres_project_media_refund_never_clamps_underfunded_counter(
    postgres_context, monkeypatch, underfunded,
):
    """FAKE PostgreSQL: malformed Sidecar replies cannot justify refunds.

    Remote bytes were written before the invalid probe was returned. Preserve
    the reservation even when it conflicts with an out-of-band damaged counter.
    This does not prove actual remote deletion or trusted reconciliation.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE direct media quota guard"})
    assert created.status_code == 201, created.text
    project = created.json()
    uploaded_bytes = b"fake-only-direct-media-evidence"
    adapter = app.state.video_use
    original_upload = adapter.upload_media
    calls = []

    def failed_probe(*args, **kwargs):
        result = original_upload(*args, **kwargs)
        calls.append(result["mediaId"])
        with sessions() as db:
            tenant = db.query(DBTenant).one()
            assert tenant.used_storage_bytes == len(uploaded_bytes)
            if underfunded:
                tenant.used_storage_bytes = 0
                db.commit()
        return {**result, "metadata": []}

    monkeypatch.setattr(adapter, "upload_media", failed_probe)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", uploaded_bytes, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert len(calls) == 1
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == (0 if underfunded else len(uploaded_bytes))
        assert db.query(DBAssetVersion).count() == 0
        project_row = db.get(DBProject, project["id"])
        assert project_row.revision == project["revision"]
        assert project_row.materials == []
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_same_project_direct_media_overlap_is_fenced(
    postgres_context, monkeypatch,
):
    """FAKE-only real PostgreSQL: serialize concurrent direct media by project.

    The first request commits its quota hold before entering a paused fake
    Sidecar. The second request has the *same* expected revision; it must be
    rejected before quota or any second remote upload, even though the row
    lock from the first reservation transaction was already released. This
    only closes the concurrent in-flight window, not post-crash reconciliation.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE direct media PG liveness"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-only-concurrent-direct-media-content"
    store = app.state.video_use
    original_upload = store.upload_media
    entered, resume = Event(), Event()
    calls = []

    def paused_upload(*args, **kwargs):
        calls.append(args[0])
        entered.set()
        assert resume.wait(timeout=20), "FAKE Sidecar must be resumed by test"
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(store, "upload_media", paused_upload)

    def post_media():
        return client.post(
            f"/projects/{project['id']}/media",
            data={"expectedRevision": str(project["revision"])},
            files={"file": ("synthetic.mp4", payload, "video/mp4")},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post_media)
        try:
            assert entered.wait(timeout=15), "first request did not reach fake Sidecar"
            started = time.monotonic()
            blocked = post_media()
            assert blocked.status_code == 503, blocked.text
            assert blocked.json()["detail"]["code"] == "STORAGE_HOLD_LOCK_CONTENDED"
            assert time.monotonic() - started < 10
            assert calls == [project["id"]], "contended request reached remote Sidecar"
            with sessions() as db:
                assert db.query(DBTenant).one().used_storage_bytes == len(payload)
                assert db.query(DBAssetVersion).count() == 0
                assert db.get(DBProject, project["id"]).revision == project["revision"]
        finally:
            resume.set()
        completed = first.result(timeout=20)

    assert completed.status_code == 201, completed.text
    assert calls == [project["id"]]
    stale = post_media()
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"
    assert calls == [project["id"]]
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 1
        saved = db.get(DBProject, project["id"])
        assert saved.revision == project["revision"] + 1
        assert len(saved.materials) == 1
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("probe_delta", [-1, 1])
def test_g70_g71_postgres_direct_media_rejects_probe_byte_drift(
    postgres_context, monkeypatch, probe_delta,
):
    """FAKE Sidecar byte-probe drift must not create an unaccounted asset.

    The API reserves the original upload byte count. Sidecar's returned
    media size is a distinct untrusted observation; a different size
    must not become the durable AssetVersion/material size. This test
    does not prove remote orphan cleanup or trusted quota reconciliation.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE probe-byte binding"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-only-sidecar-probe-size-binding"
    adapter = app.state.video_use
    original_upload = adapter.upload_media

    def drift_probe(*args, **kwargs):
        result = original_upload(*args, **kwargs)
        assert result["metadata"]["sizeBytes"] == len(payload)
        return {
            **result,
            "metadata": {
                **result["metadata"],
                "sizeBytes": len(payload) + probe_delta,
            },
        }

    monkeypatch.setattr(adapter, "upload_media", drift_probe)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"

    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("mutation", [
    "foreign_project",
    "missing_project",
    "unsafe_media_id",
    "nonscalar_media_id",
])
def test_g70_g71_postgres_direct_media_rejects_sidecar_identity_mismatch(
    postgres_context, monkeypatch, mutation,
):
    """Native PostgreSQL: wrong-project or malformed FAKE Sidecar identity
    cannot create an asset/material or consume a tenant storage reservation.

    This does not assert a trusted Sidecar issuer, cleanup of remote orphan
    media, or actual cross-tenant signed attribution.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE Sidecar identity binding"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-only-sidecar-identity-binding"
    adapter = app.state.video_use
    original_upload = adapter.upload_media

    def poisoned_identity(*args, **kwargs):
        result = original_upload(*args, **kwargs)
        if mutation == "foreign_project":
            return {**result, "projectId": str(uuid.uuid4())}
        if mutation == "missing_project":
            return {k: v for k, v in result.items() if k != "projectId"}
        if mutation == "unsafe_media_id":
            return {**result, "mediaId": "../foreign-media"}
        return {**result, "mediaId": ["not-a-media-id"]}

    monkeypatch.setattr(adapter, "upload_media", poisoned_identity)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"

    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_negative_storage_counter_blocks_artifact_reservation(
    postgres_context, monkeypatch,
):
    """Native PostgreSQL FAKE-only: an invalid negative balance is not quota.

    Before the counter guard, the atomic reservation accepted negative used
    bytes and could call Sidecar, creating an asset against corrupt accounting.
    This is a fail-closed subgate, not trusted orphan reconciliation.
    """
    client, sessions, app = postgres_context
    task_id, headers, artifact_id = _prepare_g70_g71_postgres_duplicate_intake(client)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None
        tenant_id, project_id = task.tenant_id, task.project_id
        tenant = db.get(DBTenant, tenant_id)
        assert tenant is not None
        tenant.used_storage_bytes = -7
        db.commit()
        events_before = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    denied = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=headers,
        data={"providerArtifactId": artifact_id},
        files={"file": ("synthetic.mp4", b"fake-negative-quota-artifact", "video/mp4")},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    sidecar_upload.assert_not_called()
    with sessions() as db:
        assert db.get(DBTenant, tenant_id).used_storage_bytes == -7
        assert db.get(DBGenerationTask, task_id).asset_version_id is None
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
        )).scalar_one() == events_before
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_negative_storage_counter_blocks_direct_media_reservation(
    postgres_context, monkeypatch,
):
    """Native PostgreSQL FAKE-only: direct media cannot debit corrupt quota."""
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE negative direct-media quota"})
    assert created.status_code == 201, created.text
    project = created.json()
    with sessions() as db:
        tenant = db.query(DBTenant).one()
        tenant.used_storage_bytes = -11
        db.commit()

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    denied = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", b"fake-negative-quota-direct", "video/mp4")},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    sidecar_upload.assert_not_called()
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == -11
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()

@pytest.mark.parametrize("commit_is_durable", [False, True])
def test_g70_g71_postgres_direct_media_uncertain_quota_commit_stops_before_sidecar(
    postgres_context, monkeypatch, commit_is_durable,
):
    """FAKE-only PostgreSQL: a lost reservation-COMMIT acknowledgement must
    not trigger Sidecar or a guessed quota refund.

    A pre-COMMIT error leaves no durable debit; a post-COMMIT lost ACK
    retains the durable debit, both with reconciliation-required 409.
    This is not a trusted orphan receipt or durable human recovery ACK.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE ambiguous reservation commit"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-only-direct-media-uncertain-reservation"
    original_commit = Session.commit
    commit_calls = [0]

    def lost_reservation_ack(db, *args, **kwargs):
        commit_calls[0] += 1
        # The HTTP auth dependency commits its credential/session bookkeeping
        # before the upload route runs. Fault-inject only the *second* COMMIT,
        # which is the storage reservation's first durable transaction.
        if commit_calls[0] == 2:
            if commit_is_durable:
                original_commit(db, *args, **kwargs)
            raise RuntimeError("FAKE quota reservation COMMIT acknowledgement lost")
        return original_commit(db, *args, **kwargs)

    sidecar_upload = MagicMock(wraps=app.state.video_use.upload_media)
    monkeypatch.setattr(Session, "commit", lost_reservation_ack)
    monkeypatch.setattr(app.state.video_use, "upload_media", sidecar_upload)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    )
    assert commit_calls[0] == 2
    sidecar_upload.assert_not_called()
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == (
            len(payload) if commit_is_durable else 0
        )
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["postcommit_ack_lost", "postcommit_refresh_lost"])
def test_g70_g71_postgres_direct_media_uncertain_asset_commit_keeps_storage_hold(
    postgres_context, monkeypatch, failure_stage,
):
    """FAKE-only PostgreSQL: never refund an asset whose COMMIT may be durable.

    A successful DB COMMIT may lose its acknowledgement or the subsequent
    refresh may fail. The old generic exception handler refunded the quota
    even though the asset/material had already committed. The safe response
    is reconciliation-required 409 while retaining the storage debit.
    This is NOT remote Sidecar cleanup, signed proof, or human recovery ACK.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE ambiguous asset commit"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-direct-media-commit-unknown-boundary"
    original_commit = Session.commit
    original_refresh = Session.refresh

    if failure_stage == "postcommit_ack_lost":
        def lost_commit_ack(db, *args, **kwargs):
            committing_asset = any(
                isinstance(obj, DBAssetVersion) for obj in db.new
            )
            result = original_commit(db, *args, **kwargs)
            if committing_asset:
                raise RuntimeError("FAKE lost asset COMMIT acknowledgement")
            return result

        monkeypatch.setattr(Session, "commit", lost_commit_ack)
    else:
        def lost_refresh(db, instance, *args, **kwargs):
            if isinstance(instance, DBAssetVersion):
                raise RuntimeError("FAKE post-COMMIT asset readback failure")
            return original_refresh(db, instance, *args, **kwargs)

        monkeypatch.setattr(Session, "refresh", lost_refresh)

    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    )
    with sessions() as db:
        tenant = db.query(DBTenant).one()
        assert tenant.used_storage_bytes == len(payload), (
            "Post-COMMIT failure must not refund the persisted asset"
        )
        assets = db.execute(select(DBAssetVersion).where(
            DBAssetVersion.project_id == project["id"],
        )).scalars().all()
        assert len(assets) == 1
        assert assets[0].size_bytes == len(payload)
        assert assets[0].sha256 == hashlib.sha256(payload).hexdigest()
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"] + 1
        assert len(stored_project.materials) == 1
        assert stored_project.materials[0]["id"] == assets[0].media_id
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("sidecar_wrote_bytes", [False, True])
def test_g70_g71_postgres_direct_media_ambiguous_sidecar_failure_retains_hold(
    postgres_context, monkeypatch, sidecar_wrote_bytes,
):
    """FAKE-only PostgreSQL: a Sidecar transport failure is not a storage
    non-write receipt. Even if the remote file has been durably written,
    the API can lose its ACK before the media/asset transaction begins.

    Preserve the original quota hold and return reconciliation-required;
    never issue a speculative quota refund or accept an automatic retry.
    This tests only the fail-closed boundary, not signed Sidecar evidence,
    a durable human ACK, or orphan recovery.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE uncertain Sidecar ACK"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-only-direct-media-sidecar-ack-may-be-lost"
    adapter = app.state.video_use
    original_upload = adapter.upload_media
    remote_receipts = []

    def lose_sidecar_ack(*args, **kwargs):
        if sidecar_wrote_bytes:
            result = original_upload(*args, **kwargs)
            assert result["projectId"] == project["id"]
            remote_receipts.append(result)
        raise VideoUseError("FAKE Sidecar ACK lost; storage outcome unknown")

    monkeypatch.setattr(adapter, "upload_media", lose_sidecar_ack)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == (
        "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    )
    assert len(remote_receipts) == int(sidecar_wrote_bytes)
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("remote_write_completed", [False, True])
def test_g70_g71_postgres_direct_media_generic_sidecar_exception_retains_hold(
    postgres_context, monkeypatch, remote_write_completed,
):
    """FAKE-only: non-VideoUseError after dispatch is not non-write proof.

    The Sidecar can write a durable remote object before a generic exception.
    No quota refund, asset commit, Provider call or automated retry may follow.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE generic lost Sidecar ACK"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"fake-sidecar-nonguaranteed-write-before-runtime-error"
    adapter = app.state.video_use
    original_upload = adapter.upload_media
    remote_receipts = []
    dispatches = []

    def fail_with_generic_exception(*args, **kwargs):
        dispatches.append(1)
        if remote_write_completed:
            remote_receipts.append(original_upload(*args, **kwargs))
        raise RuntimeError("FAKE Sidecar outcome uncertain after dispatch")

    monkeypatch.setattr(adapter, "upload_media", fail_with_generic_exception)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert len(dispatches) == 1
    assert len(remote_receipts) == int(remote_write_completed)
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 0
        stored_project = db.get(DBProject, project["id"])
        assert stored_project.revision == project["revision"]
        assert stored_project.materials == []
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize(
    "inspection_fault",
    ["same_size_sha_drift", "foreign_project", "foreign_media",
     "wrong_bytes", "boolean_bytes", "missing_sha", "inspect_unavailable"],
)
def test_g70_g71_postgres_direct_media_stored_sha_readback_fail_closed(
    postgres_context, monkeypatch, inspection_fault,
):
    """FAKE-only PG: equal-size Sidecar ACK is not proof of actual stored bytes.

    A failing same-Sidecar read-back leaves quota HELD, without new assets,
    materials, version edits or speculative refund. This does not certify an
    independently signed issuer or safe orphan recovery.
    """
    client, sessions, app = postgres_context
    created = client.post(
        "/projects", json={"name": "FAKE direct-media stored SHA readback"}
    )
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"g70-g71-fake-direct-media-stored-bytes-proof"
    adapter = app.state.video_use
    original_inspect = adapter.inspect_media
    calls = []

    def untrusted_inspection(project_id, media_id):
        calls.append((project_id, media_id))
        if inspection_fault == "inspect_unavailable":
            raise VideoUseError("FAKE Sidecar inspection ACK lost")
        evidence = original_inspect(project_id, media_id)
        if inspection_fault == "same_size_sha_drift":
            evidence["sha256"] = hashlib.sha256(payload[::-1]).hexdigest()
        elif inspection_fault == "foreign_project":
            evidence["projectId"] = "untrusted-foreign-project"
        elif inspection_fault == "foreign_media":
            evidence["mediaId"] = "untrusted-foreign-media"
        elif inspection_fault == "wrong_bytes":
            evidence["sizeBytes"] = len(payload) + 1
        elif inspection_fault == "boolean_bytes":
            evidence["sizeBytes"] = True
        elif inspection_fault == "missing_sha":
            evidence.pop("sha256")
        return evidence

    monkeypatch.setattr(adapter, "inspect_media", untrusted_inspection)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert len(calls) == 1
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 0
        saved = db.get(DBProject, project["id"])
        assert saved.revision == project["revision"]
        assert saved.materials == []
    app.state.moneyprinter.assert_not_called()


@pytest.mark.parametrize("remote_write_completed", [False, True])
def test_g70_g71_postgres_crash_durable_direct_media_hold_blocks_retransmission(
    postgres_context, monkeypatch, remote_write_completed,
):
    """Native PG FAKE-only: a prior UNKNOWN survives beyond the request lock.

    Replaying the *same normal upload* against the unchanged project revision
    must not reserve additional quota or dispatch to Sidecar a second time.
    This only proves a durable fail-closed fence, not a trusted orphan recovery
    proof, manual ACK, auto refund, or real SIGKILL sidecar settlement.
    """
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE durable direct media hold"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"g70-g71-fake-durable-direct-media-unknown-retry"
    adapter = app.state.video_use
    original_upload = adapter.upload_media
    calls = []

    def uncertain_upload(*args, **kwargs):
        calls.append(1)
        if remote_write_completed:
            original_upload(*args, **kwargs)
        raise VideoUseError("FAKE transport ACK lost after dispatch")

    monkeypatch.setattr(adapter, "upload_media", uncertain_upload)
    request = lambda: client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    first = request()
    assert first.status_code == 409, first.text
    assert first.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    # The first session has returned and released its PG advisory lock.
    second = request()
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert len(calls) == 1, "unreconciled second request must never reach Sidecar"
    with sessions() as db:
        tenant = db.query(DBTenant).one()
        assert tenant.used_storage_bytes == len(payload)
        holds = db.execute(select(DBDirectMediaUploadHold).where(
            DBDirectMediaUploadHold.project_id == project["id"],
        )).scalars().all()
        assert len(holds) == 1
        assert holds[0].state == "HELD"
        assert holds[0].source_sha256 == hashlib.sha256(payload).hexdigest()
        assert holds[0].size_bytes == len(payload)
        assert holds[0].media_id is None
        assert db.query(DBAssetVersion).count() == 0
        saved = db.get(DBProject, project["id"])
        assert saved.revision == project["revision"]
        assert saved.materials == []
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_postgres_direct_media_success_settles_durable_hold_atomically(
    postgres_context,
):
    """FAKE-only real PG: normal media commit must settle its original hold."""
    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE direct hold settled"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"g70-g71-fake-atomic-successful-media-hold"
    result = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", payload, "video/mp4")},
    )
    assert result.status_code == 201, result.text
    with sessions() as db:
        holds = db.execute(select(DBDirectMediaUploadHold).where(
            DBDirectMediaUploadHold.project_id == project["id"],
        )).scalars().all()
        assert len(holds) == 1
        assert holds[0].state == "SETTLED"
        assert holds[0].media_id == result.json()["assetVersion"]["mediaId"]
        assert holds[0].source_sha256 == hashlib.sha256(payload).hexdigest()
        assert db.query(DBTenant).one().used_storage_bytes == len(payload)
        assert db.query(DBAssetVersion).count() == 1
    app.state.moneyprinter.assert_not_called()
