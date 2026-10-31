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
from threading import Barrier, Event, get_ident
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.main import create_app
from app.video_use_adapter import VideoUseError
from app.models import (
    DBAssetVersion,
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
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(payload),
                "video": {"codec": "h264", "width": 320, "height": 240},
            },
        }


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
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        headers={"Idempotency-Key": key},
        json=payload,
    )
    assert response.status_code == 202, response.text
    return response.json()["taskId"]


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

def test_g72_postgres_artifact_upload_failure_compensates_storage_case(postgres_context, monkeypatch):
    """A real disposable PostgreSQL transaction executes the upload-failure CASE compensation."""
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

    def fail_upload(*_args, **_kwargs):
        raise VideoUseError("synthetic PostgreSQL sidecar upload failure")

    monkeypatch.setattr(app.state.video_use, "upload_media", fail_upload)
    response = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=worker_headers,
        data={"providerArtifactId": artifact_id},
        files={"file": ("synthetic.mp4", b"g72-postgres-failure-bytes", "video/mp4")},
    )
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORE_FAILED"

    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, tenant_id)
        assert stored.status == "FAILED"
        assert stored.error_code == "ARTIFACT_INGEST_FAILED"
        assert tenant.used_storage_bytes == 0
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_type == "video",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one() == 1
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        assert attempt.reconciliation_state == "HELD"
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
        assert stored.media_id == "_expected_media_id(project_id, f"generation:{task_id}")"
        assert stored.asset_version_id is None
        assert tenant.used_storage_bytes == 0
        orphan_event = db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one()
        assert orphan_event.metadata_json["reasonCode"] == "TASK_CANCELED"
        assert orphan_event.metadata_json["orphanedMediaId"] == "_expected_media_id(project_id, f"generation:{task_id}")"
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert orphan_event.metadata_json["originalProviderArtifactId"] == artifact_id
        assert orphan_event.metadata_json["originalUpstreamJobId"] == upstream_id
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == "_expected_media_id(project_id, f"generation:{task_id}")",
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
        assert orphan_event.metadata_json["orphanedMediaId"] == "_expected_media_id(project_id, f"generation:{task_id}")"
        assert orphan_event.metadata_json["originalProviderArtifactId"] == original_artifact_id
        assert orphan_event.metadata_json["currentTaskProviderArtifactId"] == replacement_artifact_id
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == "_expected_media_id(project_id, f"generation:{task_id}")",
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
        assert orphan_event.metadata_json["orphanedMediaId"] == "_expected_media_id(project_id, f"generation:{task_id}")"
        assert orphan_event.metadata_json["orphanedMediaSha256"] is not None
        assert orphan_event.metadata_json["orphanedMediaSizeBytes"] == len(payload)
        assert orphan_event.metadata_json["originalProviderArtifactId"] == artifact_id
        assert orphan_event.metadata_json["originalUpstreamJobId"] == upstream_id
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.media_id == "_expected_media_id(project_id, f"generation:{task_id}")",
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
            DBAssetVersion.media_id == "_expected_media_id(project_id, f"generation:{task_id}")",
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

