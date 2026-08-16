import secrets
import datetime
import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from app.auth import hash_password
from app.database import Base, build_engine
from app.main import apply_project_update, create_app
from app.migrations import ensure_schema
from app.models import DBAssetVersion, DBRenderTask, DBTenant, DBUser
from app.schemas import UpdateProjectRequest, WorkerTaskUpdateRequest
from app.task_status import canonical_task_status, database_status_values, legacy_task_status
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

OWNER_PASSWORD = secrets.token_urlsafe(24)
WORKER_TOKEN = secrets.token_urlsafe(32)
VIEWER_PASSWORD = secrets.token_urlsafe(24)
EDITOR_PASSWORD = secrets.token_urlsafe(24)
RESTART_PASSWORD = secrets.token_urlsafe(24)
RESTART_WORKER_TOKEN = secrets.token_urlsafe(32)


class FakeVideoUseAdapter:
    def __init__(self):
        self.jobs = {}
        self.status_calls = {}

    def check_health(self):
        return {"status": "healthy", "service": "video-use"}

    def get_capabilities(self):
        return {"render": True, "commit": "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"}

    def upload_media(self, project_id, filename, content_type, stream):
        assert stream.read()
        return {
            "mediaId": "media-1",
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": 1.0,
                "video": {"codec": "h264", "width": 320, "height": 240},
                "audio": {"codec": "aac", "sampleRate": 48000},
            },
        }

    def submit_render(self, payload):
        assert payload["ranges"] == [
            {
                "mediaId": "media-1",
                "start": 0.0,
                "end": 1.0,
                "note": "clip-1",
            }
        ]
        job = {
            "jobId": "11111111-1111-1111-1111-111111111111",
            "status": "queued",
            "progress": 0,
            "message": "Queued",
        }
        self.jobs[job["jobId"]] = job
        return job

    def get_job_status(self, job_id):
        calls = self.status_calls.get(job_id, 0) + 1
        self.status_calls[job_id] = calls
        completed = calls >= 2
        return {
            **self.jobs[job_id],
            "status": "completed" if completed else "processing",
            "progress": 100 if completed else 50,
            "message": "Render completed" if completed else "Rendering",
        }

    @contextmanager
    def stream(self, _path, headers=None):
        payload = b"media"
        range_header = (headers or {}).get("range")

        class Response:
            status_code = 206 if range_header else 200
            headers = {
                "accept-ranges": "bytes",
                "content-type": "video/mp4",
                "content-length": "3" if range_header else str(len(payload)),
                **({"content-range": "bytes 0-2/5"} if range_header else {}),
            }

            def iter_bytes(self):
                yield payload[:3] if range_header else payload

        yield Response()


@pytest.fixture()
def api_context(tmp_path):
    database_path = tmp_path / "aether-api-test.db"
    test_engine = build_engine(f"sqlite:///{database_path}")
    test_session = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_engine,
    )

    def get_test_db():
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    test_app = create_app(
        app_engine=test_engine,
        db_dependency=get_test_db,
        render_step_delay=0.01,
        video_use_adapter=FakeVideoUseAdapter(),
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
    )
    with TestClient(test_app) as client:
        login = client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": OWNER_PASSWORD},
        )
        assert login.status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, test_session

    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def create_project(client: TestClient, name: str = "Aether Epic Anime"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def test_health_check_uses_isolated_wal_database(api_context):
    client, _ = api_context
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["journal_mode"] == "WAL"


def test_project_list_create_query_update_and_not_found(api_context):
    client, _ = api_context
    assert client.get("/projects").json() == []

    project = create_project(client)
    project_id = project["id"]
    assert project["revision"] == 1
    assert client.get("/projects").json()[0]["id"] == project_id
    assert client.get(f"/projects/{project_id}").json()["name"] == project["name"]

    update_response = client.put(
        f"/projects/{project_id}",
        json={"name": "Aether Stylized V2", "expectedRevision": 1},
    )
    assert update_response.status_code == 200
    assert update_response.json()["revision"] == 2
    assert update_response.json()["name"] == "Aether Stylized V2"

    stale_response = client.put(
        f"/projects/{project_id}",
        json={"name": "Stale Update", "expectedRevision": 1},
    )
    assert stale_response.status_code == 409
    assert stale_response.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"

    missing_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/projects/{missing_id}").status_code == 404
    assert client.put(
        f"/projects/{missing_id}",
        json={"name": "Missing", "expectedRevision": 1},
    ).status_code == 404
    assert client.post(f"/projects/{missing_id}/render").status_code == 404


def test_auth_rbac_tenant_isolation_and_project_quota(api_context):
    client, test_session = api_context
    owner_project = create_project(client, "Owner tenant project")
    owner_upload = client.post(
        f"/projects/{owner_project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("owner.mp4", b"owner-media", "video/mp4")},
    ).json()
    owner_asset_id = owner_upload["assetVersion"]["id"]
    assert client.post(
        "/admin/users",
        json={
            "email": "viewer-same@example.com", "displayName": "Same Tenant Viewer",
            "password": VIEWER_PASSWORD, "role": "viewer",
        },
    ).status_code == 201
    same_tenant_viewer = TestClient(client.app)
    assert same_tenant_viewer.post(
        "/auth/login",
        json={"email": "viewer-same@example.com", "password": VIEWER_PASSWORD},
    ).status_code == 200
    same_tenant_viewer.headers.update({"X-Aether-CSRF": "1"})
    assert same_tenant_viewer.get(f"/projects/{owner_project['id']}").status_code == 200
    assert same_tenant_viewer.get(
        f"/projects/{owner_project['id']}/asset-versions"
    ).status_code == 200
    assert same_tenant_viewer.post(
        f"/projects/{owner_project['id']}/asset-versions/{owner_asset_id}/rights-snapshots",
        json={"status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL"},
    ).status_code == 403
    assert same_tenant_viewer.post("/projects", json={"name": "Viewer cannot create"}).status_code == 403
    assert same_tenant_viewer.put(
        f"/projects/{owner_project['id']}",
        json={"name": "Viewer cannot edit", "expectedRevision": owner_project["revision"]},
    ).status_code == 403

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    db = test_session()
    try:
        tenant = DBTenant(
            id="tenant-b", name="Tenant B", slug="tenant-b", project_quota=1,
            storage_quota_bytes=1024, used_storage_bytes=0, concurrent_render_quota=1,
            monthly_render_seconds_quota=10, render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"), created_at=now, updated_at=now,
        )
        viewer = DBUser(
            id="editor-b", tenant_id=tenant.id, email="editor@example.com", display_name="Editor",
            password_hash=hash_password(EDITOR_PASSWORD), role="editor", is_active=True,
            created_at=now, updated_at=now,
        )
        db.add_all([tenant, viewer])
        db.commit()
    finally:
        db.close()

    isolated = TestClient(client.app)
    assert isolated.get("/projects").status_code == 401
    assert isolated.post("/auth/login", json={"email": "editor@example.com", "password": EDITOR_PASSWORD}).status_code == 200
    isolated.headers.update({"X-Aether-CSRF": "1"})
    assert isolated.get("/projects").json() == []
    assert isolated.get(f"/projects/{owner_project['id']}").status_code == 404
    assert isolated.get(
        f"/projects/{owner_project['id']}/asset-versions"
    ).status_code == 404
    assert isolated.get(
        f"/projects/{owner_project['id']}/asset-versions/{owner_asset_id}/rights-check"
    ).status_code == 404
    assert isolated.post("/projects", json={"name": "Tenant B project"}).status_code == 201
    denied = isolated.post("/projects", json={"name": "Over Tenant B quota"})
    assert denied.status_code == 429


def test_atomic_optimistic_lock_allows_exactly_one_competing_update(api_context):
    client, test_session = api_context
    project = create_project(client, "Concurrency Test")
    project_id = project["id"]

    def competing_update(name: str):
        db = test_session()
        try:
            updated = apply_project_update(
                db,
                project_id,
                UpdateProjectRequest(name=name, expectedRevision=1),
            )
            return ("updated", updated.name)
        except HTTPException as exc:
            return ("conflict", exc.status_code)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(competing_update, ["Writer A", "Writer B"]))

    assert [outcome[0] for outcome in outcomes].count("updated") == 1
    assert [outcome[0] for outcome in outcomes].count("conflict") == 1
    persisted = client.get(f"/projects/{project_id}").json()
    assert persisted["revision"] == 2
    assert persisted["name"] in {"Writer A", "Writer B"}


def test_render_and_sse_emit_real_task_progress_event(api_context):
    client, _ = api_context
    project = create_project(client, "Render Test")

    upload = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
    )
    assert upload.status_code == 201
    updated_project = upload.json()["project"]
    assert updated_project["revision"] == 2
    assert updated_project["materials"][0]["id"] == "media-1"
    assert updated_project["materials"][0]["contentType"] == "video/mp4"
    media_stream = client.get(
        f"/video-use/media/{project['id']}/media-1",
    )
    assert media_stream.status_code == 200
    assert media_stream.headers["content-type"] == "video/mp4"
    assert media_stream.content == b"media"
    media_range = client.get(
        f"/video-use/media/{project['id']}/media-1",
        headers={"Range": "bytes=0-2"},
    )
    assert media_range.status_code == 206
    assert media_range.headers["accept-ranges"] == "bytes"
    assert media_range.headers["content-range"] == "bytes 0-2/5"
    assert media_range.content == b"med"

    timeline = {
        "version": "1.1",
        "tracks": [
            {
                "id": "track-1",
                "name": "Video Track 1",
                "type": "video",
                "clips": [
                    {
                        "id": "clip-1",
                        "trackId": "track-1",
                        "materialId": "media-1",
                        "start": {"value": 0, "timescale": 24000},
                        "duration": {"value": 24000, "timescale": 24000},
                        "sourceIn": {"value": 0, "timescale": 24000},
                    }
                ],
            }
        ],
    }
    update = client.put(
        f"/projects/{project['id']}",
        json={"timeline": timeline, "expectedRevision": 2},
    )
    assert update.status_code == 200

    render_response = client.post(f"/projects/{project['id']}/render")
    assert render_response.status_code == 202
    task_id = render_response.json()["taskId"]
    assert render_response.json()["mock"] is False
    assert render_response.json()["status"] == "queued"

    first_snapshot = client.get("/events?once=true")
    assert first_snapshot.status_code == 200
    assert "event: task_progress" in first_snapshot.text
    assert task_id in first_snapshot.text

    worker_headers = {
        "X-Worker-Token": WORKER_TOKEN,
        "X-Worker-Id": "test-worker-1",
    }
    claim = client.post("/internal/render-tasks/claim", headers=worker_headers)
    assert claim.status_code == 200
    claimed = claim.json()
    assert claimed["taskId"] == task_id
    assert claimed["renderPayload"]["requestId"] == task_id
    canonical = claimed["renderPayload"]["canonicalTimeline"]
    assert canonical["duration"] == {"value": 1, "timescale": 1}
    assert canonical["tracks"][0]["clips"][0]["start"] == {"value": 0, "timescale": 24000}

    processing = client.post(
        f"/internal/render-tasks/{task_id}/update",
        headers=worker_headers,
        json={
            "status": "processing", "progress": 50, "message": "Rendering",
            "upstreamJobId": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert processing.status_code == 200
    completed = client.post(
        f"/internal/render-tasks/{task_id}/update",
        headers=worker_headers,
        json={
            "status": "completed", "progress": 100, "message": "Render completed",
            "upstreamJobId": "11111111-1111-1111-1111-111111111111",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["artifactUrl"] == f"/api/renders/{task_id}/artifact"

    completed_snapshot = client.get("/events?once=true")
    assert f'"taskId":"{task_id}"' in completed_snapshot.text
    assert '"status":"completed"' in completed_snapshot.text
    persisted = client.get(f"/render-tasks?projectId={project['id']}").json()
    assert persisted[0]["status"] == "completed"
    assert client.get(f"/renders/{task_id}/artifact").content == b"media"
    artifact_range = client.get(
        f"/renders/{task_id}/artifact",
        headers={"Range": "bytes=0-2"},
    )
    assert artifact_range.status_code == 206
    assert artifact_range.headers["content-range"] == "bytes 0-2/5"
    assert artifact_range.content == b"med"


def test_render_payload_preserves_gap_multitrack_audio_and_exact_rational_time(api_context):
    client, _ = api_context
    project = create_project(client, "Canonical Timeline")
    upload = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
    ).json()
    media_id = upload["material"]["id"]
    timeline = {
        "version": "1.1",
        "tracks": [
            {
                "id": "video-a", "name": "Video A", "type": "video",
                "clips": [
                    {"id": "clip-a", "trackId": "video-a", "materialId": media_id,
                     "start": {"value": 0, "timescale": 24}, "duration": {"value": 24, "timescale": 24},
                     "sourceIn": {"value": 0, "timescale": 24}},
                    {"id": "clip-b", "trackId": "video-a", "materialId": media_id,
                     "start": {"value": 72, "timescale": 24}, "duration": {"value": 24, "timescale": 24},
                     "sourceIn": {"value": 0, "timescale": 24}},
                ],
            },
            {
                "id": "audio-a", "name": "Audio A", "type": "audio",
                "clips": [{"id": "audio-clip", "trackId": "audio-a", "materialId": media_id,
                           "start": {"value": 1, "timescale": 3}, "duration": {"value": 2, "timescale": 3},
                           "sourceIn": {"value": 0, "timescale": 24}, "volume": 0.5}],
            },
        ],
    }
    assert client.put(
        f"/projects/{project['id']}",
        json={"timeline": timeline, "expectedRevision": 2},
    ).status_code == 200
    task = client.post(f"/projects/{project['id']}/render").json()
    claim = client.post(
        "/internal/render-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "canonical-worker"},
    ).json()
    assert claim["taskId"] == task["taskId"]
    canonical = claim["renderPayload"]["canonicalTimeline"]
    assert canonical["duration"] == {"value": 4, "timescale": 1}
    assert canonical["tracks"][1]["clips"][0]["start"] == {"value": 1, "timescale": 3}


def test_project_storage_concurrency_and_render_second_quotas_are_enforced(api_context):
    client, sessions = api_context
    project = create_project(client, "Quota project")
    db = sessions()
    try:
        tenant = db.query(DBTenant).filter(DBTenant.slug == "aether-studio").one()
        tenant.project_quota = 1
        tenant.storage_quota_bytes = 3
        tenant.concurrent_render_quota = 1
        tenant.monthly_render_seconds_quota = 1
        db.commit()
    finally:
        db.close()

    assert client.post("/projects", json={"name": "Over project quota"}).status_code == 429
    storage = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", b"four", "video/mp4")},
    )
    assert storage.status_code == 429
    assert storage.json()["detail"]["code"] == "STORAGE_QUOTA_EXCEEDED"

    db = sessions()
    try:
        tenant = db.query(DBTenant).filter(DBTenant.slug == "aether-studio").one()
        tenant.storage_quota_bytes = 1024
        db.commit()
    finally:
        db.close()
    upload = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
    ).json()
    media_id = upload["material"]["id"]
    timeline = {
        "version": "1.1",
        "tracks": [{
            "id": "video", "name": "Video", "type": "video",
            "clips": [{
                "id": "clip", "trackId": "video", "materialId": media_id,
                "start": {"value": 0, "timescale": 24},
                "duration": {"value": 24, "timescale": 24},
                "sourceIn": {"value": 0, "timescale": 24},
            }],
        }],
    }
    assert client.put(
        f"/projects/{project['id']}", json={"timeline": timeline, "expectedRevision": 2}
    ).status_code == 200
    first = client.post(f"/projects/{project['id']}/render")
    assert first.status_code == 202
    concurrent = client.post(f"/projects/{project['id']}/render")
    assert concurrent.status_code == 429
    assert concurrent.json()["detail"]["code"] == "RENDER_CONCURRENCY_QUOTA_EXCEEDED"

    def competing_claim(worker_id: str):
        headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id}
        return worker_id, headers, client.post("/internal/render-tasks/claim", headers=headers)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(competing_claim, ["quota-worker-a", "quota-worker-b"]))
    assert sorted(result[2].status_code for result in claims) == [200, 204]
    winner_id, worker_headers, winner_response = next(result for result in claims if result[2].status_code == 200)
    assert winner_id in {"quota-worker-a", "quota-worker-b"}
    claim = winner_response.json()
    assert client.post(
        f"/internal/render-tasks/{claim['taskId']}/update",
        headers=worker_headers,
        json={"status": "failed", "progress": 100, "message": "Non-retryable test failure"},
    ).status_code == 200
    monthly = client.post(f"/projects/{project['id']}/render")
    assert monthly.status_code == 429
    assert monthly.json()["detail"]["code"] == "RENDER_SECONDS_QUOTA_EXCEEDED"


def test_render_task_survives_api_restart(tmp_path):
    database_path = tmp_path / "restart.db"
    test_engine = build_engine(f"sqlite:///{database_path}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def get_test_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    def make_app():
        return create_app(
            app_engine=test_engine, db_dependency=get_test_db,
            video_use_adapter=FakeVideoUseAdapter(),
            bootstrap_admin_password=RESTART_PASSWORD,
            bootstrap_admin_email="restart@example.com",
            worker_token=RESTART_WORKER_TOKEN, cookie_secure=False, enforce_csrf=True,
        )

    with TestClient(make_app()) as first_client:
        assert first_client.post("/auth/login", json={"email": "restart@example.com", "password": RESTART_PASSWORD}).status_code == 200
        first_client.headers.update({"X-Aether-CSRF": "1"})
        project = create_project(first_client, "Restart recovery")
        upload = first_client.post(
            f"/projects/{project['id']}/media", data={"expectedRevision": "1"},
            files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
        ).json()
        media_id = upload["material"]["id"]
        timeline = {
            "version": "1.1", "tracks": [{
                "id": "video", "name": "Video", "type": "video",
                "clips": [{"id": "clip", "trackId": "video", "materialId": media_id,
                           "start": {"value": 0, "timescale": 24},
                           "duration": {"value": 24, "timescale": 24},
                           "sourceIn": {"value": 0, "timescale": 24}}],
            }],
        }
        assert first_client.put(f"/projects/{project['id']}", json={"timeline": timeline, "expectedRevision": 2}).status_code == 200
        task_id = first_client.post(f"/projects/{project['id']}/render").json()["taskId"]

    with TestClient(make_app()) as restarted_client:
        assert restarted_client.post("/auth/login", json={"email": "restart@example.com", "password": RESTART_PASSWORD}).status_code == 200
        tasks = restarted_client.get(f"/render-tasks?projectId={project['id']}").json()
        assert tasks[0]["taskId"] == task_id
        assert tasks[0]["status"] == "queued"

    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def test_asset_version_hash_rights_window_and_immutability(api_context):
    client, sessions = api_context
    project = create_project(client, "Typed asset foundation")
    payload = b"immutable-media-payload"
    upload = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", payload, "video/mp4")},
    )
    assert upload.status_code == 201
    asset = upload.json()["assetVersion"]
    assert asset["versionNo"] == 1
    assert asset["sha256"] == hashlib.sha256(payload).hexdigest()
    assert client.get(f"/projects/{project['id']}/asset-versions").json() == [asset]

    missing = client.get(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-check",
        params={"purpose": "EXPORT"},
    )
    assert missing.status_code == 200
    assert missing.json()["code"] == "RIGHTS_MISSING"

    now = datetime.datetime.now(datetime.timezone.utc)
    expired = client.post(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-snapshots",
        json={
            "status": "ALLOWED",
            "purpose": "EXPORT",
            "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(days=2)).isoformat(),
            "validUntil": (now - datetime.timedelta(days=1)).isoformat(),
            "evidenceRef": "evidence://expired-test",
        },
    )
    assert expired.status_code == 201
    decision = client.get(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-check",
        params={"purpose": "EXPORT"},
    ).json()
    assert decision["allowed"] is False
    assert decision["code"] == "RIGHTS_EXPIRED"

    allowed = client.post(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-snapshots",
        json={
            "status": "ALLOWED",
            "purpose": "EXPORT",
            "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
            "evidenceRef": "evidence://owner-approved",
        },
    )
    assert allowed.status_code == 201
    assert client.get(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-check",
        params={"purpose": "EXPORT"},
    ).json()["allowed"] is True

    db = sessions()
    try:
        persisted = db.get(DBAssetVersion, asset["id"])
        persisted.sha256 = "0" * 64
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
        db.rollback()
        assert db.get(DBAssetVersion, asset["id"]).sha256 == hashlib.sha256(payload).hexdigest()
    finally:
        db.close()


def _complete_render_candidate(client: TestClient, project: dict) -> tuple[dict, dict]:
    upload = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("source.mp4", b"candidate-media", "video/mp4")},
    ).json()
    media_id = upload["material"]["id"]
    timeline = {
        "version": "1.1",
        "tracks": [{
            "id": "video", "name": "Video", "type": "video",
            "clips": [{
                "id": "clip", "trackId": "video", "materialId": media_id,
                "start": {"value": 0, "timescale": 24},
                "duration": {"value": 24, "timescale": 24},
                "sourceIn": {"value": 0, "timescale": 24},
            }],
        }],
    }
    assert client.put(
        f"/projects/{project['id']}",
        json={"timeline": timeline, "expectedRevision": 2},
    ).status_code == 200
    task = client.post(f"/projects/{project['id']}/render").json()
    assert task["canonicalStatus"] == "QUEUED"
    worker_headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "candidate-worker"}
    claim = client.post("/internal/render-tasks/claim", headers=worker_headers).json()
    assert claim["canonicalStatus"] == "RUNNING"
    completed = client.post(
        f"/internal/render-tasks/{task['taskId']}/update",
        headers=worker_headers,
        json={
            "status": "completed",
            "progress": 100,
            "message": "Render completed",
            "upstreamJobId": "22222222-2222-2222-2222-222222222222",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["canonicalStatus"] == "SUCCEEDED"
    candidates = client.get(f"/projects/{project['id']}/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["status"] == "READY"
    return upload, candidates[0]


def test_candidate_adoption_rights_idempotency_and_canonical_status(api_context):
    client, sessions = api_context
    project = create_project(client, "Candidate adoption")
    upload, candidate = _complete_render_candidate(client, project)
    asset = upload["assetVersion"]

    blocked = client.post(
        f"/projects/{project['id']}/candidates/{candidate['id']}/adopt",
        headers={"Idempotency-Key": "adopt-without-rights"},
        json={"reason": "Owner selected candidate"},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "RIGHTS_CHECK_FAILED"
    assert blocked.json()["detail"]["failures"][0]["code"] == "RIGHTS_MISSING"

    now = datetime.datetime.now(datetime.timezone.utc)
    assert client.post(
        f"/projects/{project['id']}/asset-versions/{asset['id']}/rights-snapshots",
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
            "evidenceRef": "evidence://export-approved",
        },
    ).status_code == 201
    endpoint = f"/projects/{project['id']}/candidates/{candidate['id']}/adopt"
    first = client.post(
        endpoint,
        headers={"Idempotency-Key": "adopt-candidate-0001"},
        json={"reason": "Owner selected candidate"},
    )
    assert first.status_code == 201
    assert first.json()["revisionNo"] == 1
    repeated = client.post(
        endpoint,
        headers={"Idempotency-Key": "adopt-candidate-0001"},
        json={"reason": "Owner selected candidate"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]
    conflict = client.post(
        endpoint,
        headers={"Idempotency-Key": "adopt-candidate-0002"},
        json={"reason": "Duplicate adoption must fail"},
    )
    assert conflict.status_code == 409
    masters = client.get(f"/projects/{project['id']}/masters").json()
    assert [master["id"] for master in masters] == [first.json()["id"]]

    db = sessions()
    try:
        task = db.get(DBRenderTask, candidate["taskId"])
        assert task.status == "SUCCEEDED"
    finally:
        db.close()


def test_task_status_aliases_are_read_compatible_and_new_writes_are_canonical():
    assert canonical_task_status("dispatching") == "RUNNING"
    assert canonical_task_status("completed") == "SUCCEEDED"
    assert legacy_task_status("PARTIAL") == "partial"
    assert {"queued", "QUEUED"}.issubset(database_status_values("QUEUED"))
    assert WorkerTaskUpdateRequest(
        status="processing", progress=10, message="working"
    ).status == "RUNNING"
    with pytest.raises(ValueError, match="Unsupported task status"):
        canonical_task_status("invented")


def test_additive_migration_preserves_legacy_project_and_creates_typed_tables(tmp_path):
    database_path = tmp_path / "legacy.db"
    legacy_engine = build_engine(f"sqlite:///{database_path}")
    with legacy_engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE projects (
                id VARCHAR PRIMARY KEY,
                name VARCHAR NOT NULL,
                timeline JSON NOT NULL,
                materials JSON NOT NULL,
                revision INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(
            text("""
                INSERT INTO projects
                    (id, name, timeline, materials, revision, created_at, updated_at)
                VALUES
                    ('legacy-project', 'Legacy', '{"version":"1.1","tracks":[]}', '[]', 1,
                     '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')
            """)
        )

    ensure_schema(legacy_engine)
    schema = inspect(legacy_engine)
    assert {
        "asset_versions", "rights_snapshots", "candidates", "adoptions", "master_revisions"
    }.issubset(schema.get_table_names())
    assert {"tenant_id", "owner_id"}.issubset(
        {column["name"] for column in schema.get_columns("projects")}
    )
    with legacy_engine.connect() as connection:
        assert connection.execute(
            text("SELECT name FROM projects WHERE id = 'legacy-project'")
        ).scalar_one() == "Legacy"
    legacy_engine.dispose()
