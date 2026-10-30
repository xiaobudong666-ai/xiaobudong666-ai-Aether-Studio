"""G76 PostgreSQL-only concurrency proof. FAKE-only; no Provider calls.

The ordinary API/Compose suite uses SQLite and therefore cannot prove that
PostgreSQL SELECT FOR UPDATE serializes lease validation with task mutation.
This test uses a disposable CI PostgreSQL service and verifies the actual
heartbeat endpoint waits for the task-row lock, then observes the new lease.
"""
from __future__ import annotations

import datetime
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.main import create_app
from app.models import DBGenerationTask


OWNER_PASSWORD = secrets.token_urlsafe(24)
WORKER_TOKEN = secrets.token_urlsafe(32)


class FakeMediaStore:
    def check_health(self):
        return {"status": "healthy"}

    def get_capabilities(self):
        return {"render": True}

    def upload_media(self, project_id, filename, content_type, stream):
        payload = stream.read()
        return {
            "mediaId": f"unused-{uuid.uuid4()}",
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {"durationSeconds": 1.0, "sizeBytes": len(payload)},
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


def _wait_until_postgres_reports_lock_wait(sessions, timeout_seconds: float = 8.0) -> bool:
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
                      AND query ILIKE '%generation_tasks%'
                    """
                )
            ).scalar_one()
        if waiting:
            return True
        time.sleep(0.05)
    return False


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
