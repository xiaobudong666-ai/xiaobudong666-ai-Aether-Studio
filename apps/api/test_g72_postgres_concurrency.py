"""Native PostgreSQL G72 FAKE-only concurrency checks; not a release gate.

Run ONLY against a disposable localhost PostgreSQL database. Synthetic image
and REMIX proof intentionally do not exercise trusted M12 issuance or Providers.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.main import create_app
from app.models import (
    DBAssetVersion, DBGenerationAttempt, DBGenerationEvent,
    DBGenerationTask, DBGenerationUsageEntry, DBProject, DBRightsSnapshot,
)

OWNER_PASSWORD = "ci-g72-postgres-test-only-password"
WORKER_TOKEN = "ci-g72-postgres-test-only-worker-token"


class NoUploadMedia:
    def __init__(self):
        self.uploads = 0

    def check_health(self):
        return {"status": "healthy"}

    def get_capabilities(self):
        return {"render": True}

    def upload_media(self, *args, **kwargs):
        self.uploads += 1
        raise AssertionError("No physical upload permitted in PG concurrency test")


@pytest.fixture
def pg_fake_context(monkeypatch):
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    url = os.environ["G72_PG_CI_DATABASE_URL"]
    parsed = urlsplit(url)
    assert parsed.scheme == "postgresql+psycopg", "PostgreSQL driver required"
    assert parsed.hostname in {"localhost", "127.0.0.1"}, "localhost only"
    assert parsed.path == "/aether_g72_ci", "disposable CI database only"
    monkeypatch.setenv("AETHER_MAX_UPLOAD_BYTES", "1024")
    engine = build_engine(url)
    assert engine.dialect.name == "postgresql"
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        with sessions() as db:
            yield db

    store = NoUploadMedia()
    app = create_app(
        app_engine=engine, db_dependency=get_db, video_use_adapter=store,
        bootstrap_admin_email="ci-g72-pg-owner@example.invalid",
        bootstrap_admin_password=OWNER_PASSWORD,
        worker_token=WORKER_TOKEN, cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    try:
        with TestClient(app) as client:
            login = client.post("/auth/login", json={
                "email": "ci-g72-pg-owner@example.invalid",
                "password": OWNER_PASSWORD,
            })
            assert login.status_code == 200, login.text
            client.headers["X-Aether-CSRF"] = "1"
            yield client, sessions, store
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def make_task_request(client, sessions, *, key: str):
    project_reply = client.post("/projects", json={"name": "CI-G72 PG " + uuid.uuid4().hex})
    assert project_reply.status_code == 201, project_reply.text
    project = project_reply.json()
    now = dt.datetime.now(dt.timezone.utc)
    with sessions() as db:
        stored_project = db.get(DBProject, project["id"])
        assert stored_project is not None
        source = DBAssetVersion(
            id=str(uuid.uuid4()), tenant_id=stored_project.tenant_id,
            project_id=stored_project.id, media_id="synthetic-ci-g72-pg-" + uuid.uuid4().hex,
            version_no=1, sha256="a" * 64, media_type="image",
            content_type="image/png", size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=stored_project.owner_id, created_at=now,
        )
        db.add(source)
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=stored_project.tenant_id,
            project_id=stored_project.id, asset_version_id=source.id,
            status="ALLOWED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_CI_ONLY_NO_M12_TRUST",
            captured_by=stored_project.owner_id, captured_at=now,
        ))
        db.commit()
        source_id = source.id
    capabilities = client.get("/generation/providers/moneyprinter/capabilities")
    assert capabilities.status_code == 200, capabilities.text
    payload = {
        "text": "Synthetic FAKE-only PG concurrency clip", "durationSeconds": 1,
        "aspectRatio": "9:16", "resolution": "720p", "voiceId": "synthetic-seed",
        "imageAssetVersionId": source_id, "idempotencyKey": key,
        "expectedProjectRevision": project["revision"],
        "capabilitySnapshotHash": capabilities.json()["snapshotHash"],
        "confirmSyntheticTestOnly": True,
    }
    return project["id"], source_id, payload


def claim(client, worker):
    return client.post("/internal/generation-tasks/claim", headers={
        "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker,
    })


def test_g72_pg_parallel_same_owner_idempotency_and_claim(pg_fake_context):
    client, sessions, store = pg_fake_context
    project, source_id, payload = make_task_request(client, sessions, key=str(uuid.uuid4()))
    endpoint = f"/projects/{project}/talking-head/fake-generation-tasks"
    with ThreadPoolExecutor(max_workers=4) as executor:
        replies = list(executor.map(lambda _: client.post(endpoint, json=payload), range(4)))
    assert all(r.status_code == 202 for r in replies), [(r.status_code, r.text) for r in replies]
    task_ids = {r.json()["taskId"] for r in replies}
    assert len(task_ids) == 1, task_ids
    task_id = task_ids.pop()
    with ThreadPoolExecutor(max_workers=6) as executor:
        claims = list(executor.map(lambda n: claim(client, f"ci-pg-worker-{n}"), range(6)))
    assert [r.status_code for r in claims].count(200) == 1, [
        (r.status_code, r.text) for r in claims
    ]
    assert all(r.status_code in (200, 204) for r in claims)
    assert next(r for r in claims if r.status_code == 200).json()["taskId"] == task_id
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task.status == "SUBMITTING" and task.lease_owner
        assert task.requested_by and task.request_json["imageAssetId"] == source_id
        assert db.execute(select(func.count(DBGenerationTask.id)).where(
            DBGenerationTask.idempotency_key == payload["idempotencyKey"]
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == task_id
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "TASK_CLAIMED"
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED"
        )).scalar_one() == 1
    assert store.uploads == 0


def test_g72_pg_revoke_between_claim_and_intake_fails_closed(pg_fake_context):
    client, sessions, store = pg_fake_context
    project, source_id, payload = make_task_request(client, sessions, key=str(uuid.uuid4()))
    created = client.post(f"/projects/{project}/talking-head/fake-generation-tasks", json=payload)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    acquired = claim(client, "ci-pg-owner-worker")
    assert acquired.status_code == 200 and acquired.json()["taskId"] == task_id, acquired.text
    with sessions() as db:
        original = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == source_id
        )).scalar_one()
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=original.tenant_id,
            project_id=original.project_id, asset_version_id=source_id,
            status="REVOKED", purpose="REMIX", territory="TEST",
            valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_CI_REVOKED_AFTER_PG_CLAIM",
            captured_by=original.captured_by,
            captured_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2),
        ))
        db.commit()
    denied = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "ci-pg-owner-worker"},
        data={"providerArtifactId": "ci-pg-synthetic-artifact"},
        files={"file": ("synthetic.mp4", b"not-a-real-mp4", "video/mp4")},
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task.asset_version_id is None and task.media_id is None
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind.in_(["SETTLED", "RELEASED"])
        )).scalar_one() == 0
    assert store.uploads == 0


def _pg_claim_in_independent_process(url, worker_id, ready, go, results):
    """Separate OS process, SQLAlchemy pool and FastAPI lifespan; synthetic only."""
    import traceback

    engine = None
    announced = False
    try:
        engine = build_engine(url)
        sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

        def get_db():
            with sessions() as db:
                yield db

        app = create_app(
            app_engine=engine, db_dependency=get_db,
            video_use_adapter=NoUploadMedia(),
            bootstrap_admin_email="ci-g72-pg-owner@example.invalid",
            bootstrap_admin_password=OWNER_PASSWORD,
            worker_token=WORKER_TOKEN, cookie_secure=False, enforce_csrf=True,
            generation_provider_mode="deterministic-fake",
        )
        with TestClient(app) as client:
            ready.put((worker_id, "READY"))
            announced = True
            if not go.wait(timeout=30):
                raise RuntimeError("CI-only process-claim synchronization timed out")
            reply = claim(client, worker_id)
            results.put((worker_id, reply.status_code, reply.text[:300]))
    except Exception:
        error = (worker_id, "ERROR", traceback.format_exc()[-1800:])
        (results if announced else ready).put(error)
    finally:
        if engine is not None:
            engine.dispose()


def test_g72_pg_independent_worker_processes_claim_once(pg_fake_context):
    """Native PG claim arbitration across isolated API/Worker OS processes.

    Every child constructs its own FastAPI application and database connection
    pool; the task, fake actor and REMIX authorization are synthetic.  This
    does NOT validate a physical Sidecar, M12 issuance or delegated admins.
    """
    import multiprocessing

    client, sessions, store = pg_fake_context
    project, _, payload = make_task_request(
        client, sessions, key=str(uuid.uuid4())
    )
    created = client.post(
        f"/projects/{project}/talking-head/fake-generation-tasks",
        json=payload,
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    assert os.environ["AETHER_FAKE_ONLY_NONPROD"] == "true"
    url = os.environ["G72_PG_CI_DATABASE_URL"]

    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    go = context.Event()
    workers = [
        context.Process(
            target=_pg_claim_in_independent_process,
            args=(url, f"ci-pg-process-{i}", ready, go, results),
        )
        for i in range(4)
    ]
    try:
        for worker in workers:
            worker.start()
        announced = [ready.get(timeout=45) for _ in workers]
        assert all(message[1] == "READY" for message in announced), announced
        go.set()
        outcomes = [results.get(timeout=45) for _ in workers]
    finally:
        go.set()
        for worker in workers:
            worker.join(timeout=8)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=8)

    assert all(worker.exitcode == 0 for worker in workers), [
        worker.exitcode for worker in workers
    ]
    assert all(isinstance(result[1], int) for result in outcomes), outcomes
    assert [result[1] for result in outcomes].count(200) == 1, outcomes
    assert all(result[1] in {200, 204} for result in outcomes), outcomes
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task is not None and task.status == "SUBMITTING"
        assert task.lease_owner in {item[0] for item in outcomes}
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "TASK_CLAIMED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == task_id,
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
    assert store.uploads == 0


def test_g72_pg_health_uses_postgres_probe(pg_fake_context):
    """PG API liveness must never execute SQLite-only PRAGMA statements."""
    client, _sessions, _store = pg_fake_context
    response = client.get("/health")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "healthy"
    assert response.json()["database"] == "postgresql"
    assert response.json()["journal_mode"] is None

