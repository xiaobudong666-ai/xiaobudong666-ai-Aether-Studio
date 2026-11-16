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
@pytest.mark.parametrize("crash_phase", ["before_object_write", "during_upload", "after_upload_ack"])
def test_g70_g71_direct_media_actual_api_sigkill_then_new_process_replay_is_fenced(
    postgres_context, crash_phase,
):
    """Crash after quota COMMIT, before object write, during upload, or after FAKE Sidecar ACK.

    A different fresh interpreter replays the normal authenticated media POST.
    The after-upload-ACK case blocks during the subsequent stored SHA inspection,
    proving HELD survives a real process death after remote upload returned.
    This is NOT an independently signed Sidecar proof or trusted reconciliation.
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
    # CRASH mode leaves the real HTTP request blocked before or after
    # FAKE Sidecar object creation; parent then issues actual OS SIGKILL.
    # REPLAY mode restarts a new API and checks the unchanged revision.
    child_script = r"""
import io
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
        if os.environ["AETHER_TEST_CRASH_PHASE"] == "before_object_write":
            # API durable quota+HELD commit is already complete, but the
            # FAKE Sidecar has not written an object. Stay fail-closed.
            Path(os.environ["AETHER_TEST_MEDIA_READY"]).write_text("sidecar-entered-prewrite")
            while True:
                time.sleep(0.1)
        blob = stream.read()
        Path(os.environ["AETHER_TEST_MEDIA_OBJECT"]).write_bytes(blob)
        if os.environ["AETHER_TEST_CRASH_PHASE"] == "during_upload":
            Path(os.environ["AETHER_TEST_MEDIA_READY"]).write_text("sidecar-write-before-ack")
            while True:
                time.sleep(0.1)
        # Complete the FAKE upload and return the ACK; the API now moves
        # to inspect_media. This models the later post-upload crash window.
        return super().upload_media(
            project_id, filename, content_type, io.BytesIO(blob), **kwargs
        )

    def inspect_media(self, project_id, media_id):
        inspected = super().inspect_media(project_id, media_id)
        if (
            os.environ["AETHER_TEST_MEDIA_STAGE"] == "CRASH"
            and os.environ["AETHER_TEST_CRASH_PHASE"] == "after_upload_ack"
        ):
            Path(os.environ["AETHER_TEST_MEDIA_READY"]).write_text("upload-acked-inspection")
            while True:
                time.sleep(0.1)
        return inspected

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
        files={"file": ("synthetic.mp4", (
            b"g70-g71-different-file-after-crash" if (
                os.environ["AETHER_TEST_MEDIA_STAGE"] == "REPLAY"
                and os.environ.get("AETHER_TEST_MEDIA_REPLAY_VARIANT") == "different"
            ) else b"g70-g71-direct-media-real-process-kill-fake-sidecar"
        ), "video/mp4")},
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
            "AETHER_TEST_CRASH_PHASE": crash_phase,
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
            if crash_phase == "before_object_write":
                assert not media.exists(), "pre-write crash must not fabricate a FAKE remote object"
            else:
                assert media.read_bytes() == payload
            assert ready.read_text() == {
                "before_object_write": "sidecar-entered-prewrite",
                "during_upload": "sidecar-write-before-ack",
                "after_upload_ack": "upload-acked-inspection",
            }[crash_phase]
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

        # Project-scoped HELD must not freeze unrelated work in the SAME tenant.
        # This is not an Owner-authorized reconciliation: the orphan stays HELD.
        other_created = client.post(
            "/projects", json={"name": "FAKE unrelated project during HELD"}
        )
        assert other_created.status_code == 201, other_created.text
        other_project = other_created.json()
        other_payload = b"g70-g71-unrelated-project-upload-while-first-hold-is-orphaned"
        other_result = client.post(
            f"/projects/{other_project['id']}/media",
            data={"expectedRevision": str(other_project["revision"])},
            files={"file": ("other-synthetic.mp4", other_payload, "video/mp4")},
        )
        assert other_result.status_code == 201, other_result.text
        with sessions() as db:
            original_hold = db.execute(select(DBDirectMediaUploadHold).where(
                DBDirectMediaUploadHold.project_id == project["id"],
            )).scalar_one()
            other_hold = db.execute(select(DBDirectMediaUploadHold).where(
                DBDirectMediaUploadHold.project_id == other_project["id"],
            )).scalar_one()
            assert original_hold.state == "HELD"
            assert other_hold.state == "SETTLED"
            assert original_hold.tenant_id == other_hold.tenant_id == tenant_id
            assert db.get(DBTenant, tenant_id).used_storage_bytes == (
                original_used + len(payload) + len(other_payload)
            )
            other_assets = db.execute(select(DBAssetVersion).where(
                DBAssetVersion.project_id == other_project["id"],
            )).scalars().all()
            assert len(other_assets) == 1
            assert other_assets[0].sha256 == hashlib.sha256(other_payload).hexdigest()

        # A genuinely new interpreter and API engine repeat the authenticated
        # normal HTTP request. The HELD receipt must fence dispatch *before*
        # quota mutation, even though the crashed process's PG lock is gone.
        env["AETHER_TEST_MEDIA_STAGE"] = "REPLAY"
        # A manual retry may supply different media bytes rather than resend
        # the original file. In both cases the durable HELD record must fence
        # a *fresh* process before a second Sidecar upload or quota debit.
        # Do not mistake this local FAKE result for a trusted recovery ACK.
        for replay_variant in ("original", "different"):
            env["AETHER_TEST_MEDIA_REPLAY_VARIANT"] = replay_variant
            env["AETHER_TEST_REPLAY_RESULT"] = str(root / f"replay-{replay_variant}.json")
            replay = subprocess.run(
                [sys.executable, "-u", "-c", child_script],
                env=env, capture_output=True, text=True, timeout=25,
            )
            assert replay.returncode == 0, replay.stderr[-2000:]
            variant_result = Path(env["AETHER_TEST_REPLAY_RESULT"])
            assert variant_result.exists(), replay.stderr[-1200:]
            result = json.loads(variant_result.read_text())
            assert result["status"] == 409, (replay_variant, result)
            assert result["detail"]["detail"]["code"] == (
                "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
            ), (replay_variant, result)
            assert not unexpected_dispatch.exists(), (
                f"{replay_variant} replay made a second FAKE Sidecar upload"
            )
        if crash_phase == "before_object_write":
            assert not media.exists(), "replay must not create the object while HELD is unresolved"
        else:
            assert media.read_bytes() == payload

        with sessions() as db:
            assert db.get(DBTenant, tenant_id).used_storage_bytes == (
                original_used + len(payload) + len(other_payload)
            )
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
