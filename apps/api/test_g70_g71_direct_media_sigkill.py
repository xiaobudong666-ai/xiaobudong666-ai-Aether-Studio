"""FAKE-only native PG: direct-media API process SIGKILL and fresh-process replay.

Nonproduction proof only. The local file is a Fake Sidecar object, not
independently signed remote-storage evidence. It cannot authorize reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import pytest
from sqlalchemy import select

from app.models import DBAssetVersion, DBDirectMediaUploadHold, DBProject, DBTenant
from test_g76_postgres_concurrency import postgres_context, OWNER_PASSWORD


# Test imports postgres_context as a fixture from the existing isolated PG suite.
def test_g70_g71_direct_media_actual_api_sigkill_then_new_process_replay_is_fenced(
    postgres_context,
):
    """Crash after quota COMMIT and FAKE Sidecar write; no second debit or dispatch.

    The first process is SIGKILLed inside the normal POST /projects/{id}/media
    route; a *different* fresh interpreter creates the app and sends the same
    authenticated HTTP request. No Provider or production storage is used.
    """
    if sys.platform != "linux":
        pytest.skip("real SIGKILL process proof needs disposable Linux CI")

    client, sessions, app = postgres_context
    created = client.post("/projects", json={"name": "FAKE direct-media OS crash replay"})
    assert created.status_code == 201, created.text
    project = created.json()
    payload = b"g70-g71-direct-media-real-process-kill-fake-sidecar"
    with sessions() as db:
        project_row = db.get(DBProject, project["id"])
        assert project_row is not None
        tenant_id, owner_id = project_row.tenant_id, project_row.owner_id
        original_used = db.get(DBTenant, tenant_id).used_storage_bytes
        assert db.execute(select(DBDirectMediaUploadHold).where(
            DBDirectMediaUploadHold.project_id == project["id"]
        )).scalars().all() == []

    # A child creates its *own* API, auth session and database engine.
    # CRASH mode leaves the real HTTP request blocked after the Sidecar
    # has written FAKE bytes, then the parent issues actual OS SIGKILL.
    # REPLAY mode restarts a new API and checks the unchanged revision.
    child_script = r"""
import json
import os
from pathlib import Path
import time
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from app.database import build_engine
from app.main import create_app
from test_g76_postgres_concurrency import FakeMediaStore

class CrashOrRejectFakeSidecar(FakeMediaStore):
    def upload_media(self, project_id, filename, content_type, stream, **kwargs):
        if os.environ["AETHER_TEST_MEDIA_STAGE"] == "REPLAY":
            Path(os.environ["AETHER_TEST_DISPATCH_MARKER"]).write_text("unexpected dispatch")
            raise AssertionError("unreconciled upload reached Sidecar twice")
        blob = stream.read()
        Path(os.environ["AETHER_TEST_MEDIA_OBJECT"]).write_bytes(blob)
        Path(os.environ["AETHER_TEST_MEDIA_READY"]).write_text("written")
        while True:
            time.sleep(0.1)

engine = build_engine(os.environ["AETHER_TEST_POSTGRES_URL"])
sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
def get_db():
    with sessions() as db:
        yield db

app = create_app(
    app_engine=engine,
    db_dependency=get_db,
    video_use_adapter=CrashOrRejectFakeSidecar(),
    bootstrap_admin_password=os.environ["AETHER_TEST_MEDIA_OWNER_PASSWORD"],
    bootstrap_admin_email="g76-postgres-owner@example.com",
    worker_token=os.environ["AETHER_TEST_MEDIA_WORKER_TOKEN"],
    cookie_secure=False,
    enforce_csrf=True,
    generation_provider_mode="deterministic-fake",
)
with TestClient(app) as client:
    login = client.post("/auth/login", json={
        "email": "g76-postgres-owner@example.com",
        "password": os.environ["AETHER_TEST_MEDIA_OWNER_PASSWORD"],
    })
    assert login.status_code == 200, login.text
    client.headers.update({"X-Aether-CSRF": "1"})
    response = client.post(
        "/projects/" + os.environ["AETHER_TEST_MEDIA_PROJECT"] + "/media",
        data={"expectedRevision": os.environ["AETHER_TEST_MEDIA_REVISION"]},
        files={"file": ("synthetic.mp4", b"g70-g71-direct-media-real-process-kill-fake-sidecar", "video/mp4")},
    )
    if os.environ["AETHER_TEST_MEDIA_STAGE"] == "REPLAY":
        Path(os.environ["AETHER_TEST_REPLAY_RESULT"]).write_text(
            json.dumps({"status": response.status_code, "detail": response.json()})
        )
engine.dispose()
"""
    from test_g76_postgres_concurrency import WORKER_TOKEN
    with tempfile.TemporaryDirectory(prefix="aether-direct-media-oskill-fake-") as tempdir:
        root = Path(tempdir)
        ready = root / "remote-object-ready"
        media = root / "fake-sidecar-object"
        replay_result = root / "replay.json"
        unexpected_dispatch = root / "unexpected-dispatch"
        env = os.environ.copy()
        env.update({
            "AETHER_TEST_MEDIA_PROJECT": project["id"],
            "AETHER_TEST_MEDIA_REVISION": str(project["revision"]),
            "AETHER_TEST_MEDIA_OWNER_PASSWORD": OWNER_PASSWORD,
            "AETHER_TEST_MEDIA_WORKER_TOKEN": WORKER_TOKEN,
            "AETHER_TEST_MEDIA_READY": str(ready),
            "AETHER_TEST_MEDIA_OBJECT": str(media),
            "AETHER_TEST_REPLAY_RESULT": str(replay_result),
            "AETHER_TEST_DISPATCH_MARKER": str(unexpected_dispatch),
            "AETHER_TEST_MEDIA_STAGE": "CRASH",
        })
        crash = subprocess.Popen(
            [sys.executable, "-u", "-c", child_script],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while not ready.exists() and crash.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.exists():
                if crash.poll() is None:
                    pytest.fail("child API did not reach FAKE Sidecar before timeout")
                _, err = crash.communicate(timeout=5)
                pytest.fail("first API process exited before quota/remote write: " + err[-1500:])
            assert crash.poll() is None
            assert media.read_bytes() == payload
            os.kill(crash.pid, signal.SIGKILL)
            assert crash.wait(timeout=10) == -signal.SIGKILL
        finally:
            if crash.poll() is None:
                crash.kill()
                crash.wait(timeout=10)
            crash.communicate(timeout=5)

        with sessions() as db:
            tenant = db.get(DBTenant, tenant_id)
            holds = db.execute(select(DBDirectMediaUploadHold).where(
                DBDirectMediaUploadHold.project_id == project["id"]
            )).scalars().all()
            assert tenant.used_storage_bytes == original_used + len(payload)
            assert len(holds) == 1
            hold = holds[0]
            assert hold.state == "HELD"
            assert hold.tenant_id == tenant_id
            assert hold.requested_by == owner_id
            assert hold.project_revision == project["revision"]
            assert hold.size_bytes == len(payload)
            assert hold.source_sha256 == hashlib.sha256(payload).hexdigest()
            assert hold.media_id is None
            assert db.execute(select(DBAssetVersion).where(
                DBAssetVersion.project_id == project["id"]
            )).scalars().all() == []

        # A genuinely new interpreter and API engine repeat the authenticated
        # normal HTTP request. The HELD receipt must fence dispatch *before*
        # quota mutation, even though the crashed process's PG lock is gone.
        env["AETHER_TEST_MEDIA_STAGE"] = "REPLAY"
        replay = subprocess.run(
            [sys.executable, "-u", "-c", child_script],
            env=env, capture_output=True, text=True, timeout=25,
        )
        assert replay.returncode == 0, replay.stderr[-2000:]
        assert replay_result.exists(), replay.stderr[-1200:]
        result = json.loads(replay_result.read_text())
        assert result["status"] == 409, result
        assert result["detail"]["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED", result
        assert not unexpected_dispatch.exists(), "replay made a second FAKE Sidecar upload"
        assert media.read_bytes() == payload

        with sessions() as db:
            assert db.get(DBTenant, tenant_id).used_storage_bytes == original_used + len(payload)
            holds = db.execute(select(DBDirectMediaUploadHold).where(
                DBDirectMediaUploadHold.project_id == project["id"]
            )).scalars().all()
            assert len(holds) == 1 and holds[0].state == "HELD"
            saved = db.get(DBProject, project["id"])
            assert saved.revision == project["revision"]
            assert saved.materials == []
            assert db.execute(select(DBAssetVersion).where(
                DBAssetVersion.project_id == project["id"]
            )).scalars().all() == []
    app.state.moneyprinter.assert_not_called()
