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
from app.models import DBAssetVersion, DBDirectMediaUploadHold, DBProject, DBRenderTask, DBTenant, DBUser
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
        self.media_objects = {}

    def check_health(self):
        return {"status": "healthy", "service": "video-use"}

    def get_capabilities(self):
        return {"render": True, "commit": "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"}

    def upload_media(self, project_id, filename, content_type, stream):
        payload = stream.read()
        assert payload
        self.media_objects[(project_id, "media-1")] = payload
        if filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return {
                "mediaId": "media-1",
                "projectId": project_id,
                "fileName": filename,
                "contentType": content_type,
                "mediaType": "image",
                "metadata": {
                    "durationSeconds": None,
                    "sizeBytes": len(payload),
                    "format": "png_pipe",
                    "video": {"codec": "png", "width": 48, "height": 48},
                    "audio": None,
                },
            }
        return {
            "mediaId": "media-1",
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "mediaType": "video",
            "metadata": {
                "durationSeconds": 1.0,
                "sizeBytes": len(payload),
                "format": "mov,mp4,m4a,3gp,3g2,mj2",
                "video": {"codec": "h264", "width": 320, "height": 240},
                "audio": {"codec": "aac", "sampleRate": 48000},
            },
        }

    def inspect_media(self, project_id, media_id):
        payload = self.media_objects.get((project_id, media_id))
        if payload is None:
            raise RuntimeError("FAKE media not found")
        return {"projectId": project_id, "mediaId": media_id,
                "sizeBytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}

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



@pytest.mark.parametrize(
    "corrupt_materials", [
        {"legacy": "invalid object root"},
        ["invalid string row"],
        [None],
        None,  # JSON null is not a safe empty catalog; *project.materials would fail after Sidecar.
    ],
)
def test_g70_g71_direct_media_corrupt_materials_fail_before_sidecar_and_quota(
    api_context, monkeypatch, corrupt_materials,
):
    """FAKE-only: historical malformed catalog must not create a new remote orphan.

    No Sidecar dispatch, storage reservation, asset, or revision mutation is
    allowed. This is not trusted reconciliation of any existing HELD record.
    """
    client, sessions = api_context
    project = create_project(client, "G70 corrupted historic materials")
    with sessions() as db:
        record = db.get(DBProject, project["id"])
        record.materials = corrupt_materials
        tenant_id = record.tenant_id
        before_bytes = db.get(DBTenant, tenant_id).used_storage_bytes
        db.commit()

    sidecar_calls = []
    adapter = client.app.state.video_use

    def forbidden_dispatch(*args, **kwargs):
        sidecar_calls.append(True)
        raise AssertionError("corrupt project must be rejected before Sidecar")

    monkeypatch.setattr(adapter, "upload_media", forbidden_dispatch)
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("synthetic.mp4", b"FAKE-only synthetic bytes", "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PROJECT_MATERIALS_INTEGRITY_REVIEW_REQUIRED"
    assert sidecar_calls == []
    with sessions() as db:
        saved = db.get(DBProject, project["id"])
        assert saved.materials == corrupt_materials
        assert saved.revision == project["revision"]
        assert db.get(DBTenant, tenant_id).used_storage_bytes == before_bytes
        assert db.query(DBDirectMediaUploadHold).filter(
            DBDirectMediaUploadHold.project_id == project["id"]
        ).count() == 0
        assert db.query(DBAssetVersion).filter(
            DBAssetVersion.project_id == project["id"]
        ).count() == 0


@pytest.mark.parametrize(
    "malformed_probe",
    [
        "video_descriptor_is_string",
        "trackless_media",
        "video_negative_width",
        "audio_descriptor_is_string",
        "video_missing_codec",
        "video_codec_not_scalar",
        "audio_missing_codec",
        "audio_codec_not_scalar",
        "media_type_contradicts_tracks",
        "media_type_is_not_scalar",
    ],
)
def test_g70_g71_direct_media_malformed_sidecar_probe_keeps_hold_without_asset(
    api_context, monkeypatch, malformed_probe,
):
    """G70/G71 FAKE-only: an ACK and matching bytes do not prove codec/track shape.

    Existing policy: after dispatch, reject unsafe probe before asset registration;
    retain the single durable HELD reservation and fence any repeated upload.
    No trusted receipt, cancellation refund, or real Provider is simulated.
    """
    client, sessions = api_context
    project = create_project(client, "G70 malformed FAKE Sidecar track")
    media_store = client.app.state.video_use
    original_upload = media_store.upload_media
    dispatch_count = 0

    def corrupted_upload(project_id, filename, content_type, stream, **kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        reply = original_upload(project_id, filename, content_type, stream, **kwargs)
        metadata = reply["metadata"]
        if malformed_probe == "video_descriptor_is_string":
            metadata["video"] = "not-a-video-track-object"
        elif malformed_probe == "trackless_media":
            metadata["video"] = None
            metadata["audio"] = None
        elif malformed_probe == "video_negative_width":
            metadata["video"]["width"] = -1
        elif malformed_probe == "video_missing_codec":
            metadata["video"].pop("codec")
        elif malformed_probe == "video_codec_not_scalar":
            metadata["video"]["codec"] = ["h264"]
        elif malformed_probe == "audio_missing_codec":
            metadata["audio"].pop("codec")
        elif malformed_probe == "audio_codec_not_scalar":
            metadata["audio"]["codec"] = ["aac"]
        elif malformed_probe == "media_type_contradicts_tracks":
            reply["mediaType"] = "audio"
        elif malformed_probe == "media_type_is_not_scalar":
            reply["mediaType"] = ["video"]
        else:
            metadata["audio"] = "not-an-audio-track-object"
        return reply

    monkeypatch.setattr(media_store, "upload_media", corrupted_upload)
    payload = b"synthetic-direct-media-invalid-sidecar-track"
    path = f"/projects/{project['id']}/media"
    request_data = {"expectedRevision": str(project["revision"])}
    upload_file = {"file": ("untrusted.mp4", payload, "video/mp4")}
    first = client.post(path, data=request_data, files=upload_file)
    assert first.status_code == 409, first.text
    assert first.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert dispatch_count == 1
    assert client.get(f"/projects/{project['id']}/asset-versions").json() == []
    current_project = client.get(f"/projects/{project['id']}").json()
    assert current_project["revision"] == 1
    assert current_project["materials"] == []

    with sessions() as db:
        holds = db.query(DBDirectMediaUploadHold).filter(
            DBDirectMediaUploadHold.project_id == project["id"],
        ).all()
        assert len(holds) == 1
        assert holds[0].state == "HELD"
        assert holds[0].size_bytes == len(payload)
        assert holds[0].source_sha256 == hashlib.sha256(payload).hexdigest()
        tenant = db.get(DBTenant, holds[0].tenant_id)
        assert tenant.used_storage_bytes == len(payload)

    replay = client.post(path, data=request_data, files=upload_file)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    assert dispatch_count == 1, "HELD must prevent a second Sidecar dispatch"
    with sessions() as db:
        holds = db.query(DBDirectMediaUploadHold).filter(
            DBDirectMediaUploadHold.project_id == project["id"],
        ).all()
        assert len(holds) == 1 and holds[0].state == "HELD"
        assert db.get(DBTenant, holds[0].tenant_id).used_storage_bytes == len(payload)


@pytest.mark.parametrize(
    ("filename", "caller_mime", "expected_mime"),
    [
        ("spoofed-video.mp4", "text/html", "video/mp4"),
        ("spoofed-image.png", "image/svg+xml", "image/png"),
    ],
)
def test_g70_g71_untrusted_multipart_mime_cannot_poison_catalog_or_download(
    api_context, monkeypatch, filename, caller_mime, expected_mime,
):
    """FAKE-only: validated track type controls asset MIME, not caller/Sidecar headers."""
    client, sessions = api_context
    project = create_project(client, "G70 MIME evidence boundary")
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": (filename, b"fake-media-mime-probe", caller_mime)},
    )
    assert response.status_code == 201, response.text
    asset = response.json()["assetVersion"]
    material = response.json()["material"]
    assert material["contentType"] == expected_mime
    assert asset["contentType"] == expected_mime
    with sessions() as db:
        stored = db.get(DBAssetVersion, asset["id"])
        assert stored is not None and stored.content_type == expected_mime

    original_stream = client.app.state.video_use.stream

    @contextmanager
    def spoofed_upstream_content_type(path, headers=None):
        with original_stream(path, headers=headers) as upstream:
            upstream.headers["content-type"] = "text/html"
            yield upstream

    monkeypatch.setattr(client.app.state.video_use, "stream", spoofed_upstream_content_type)
    download = client.get(f"/video-use/media/{project['id']}/{material['id']}")
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == expected_mime
    assert download.headers["x-content-type-options"] == "nosniff"

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
    # A completed task cannot be downloaded before source EXPORT approval.
    rights_denied = client.get(f"/renders/{task_id}/artifact")
    assert rights_denied.status_code == 403
    assert rights_denied.json()["detail"]["code"] == "RIGHTS_CHECK_FAILED"
    now = datetime.datetime.now(datetime.timezone.utc)
    rights_grant = client.post(
        f"/projects/{project['id']}/asset-versions/{upload.json()['assetVersion']['id']}/rights-snapshots",
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
            "evidenceRef": "evidence://fake-render-source-export",
        },
    )
    assert rights_grant.status_code == 201, rights_grant.text
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


def _complete_render_candidate(client: TestClient, project: dict, *, include_text_subtitle: bool = False) -> tuple[dict, dict]:
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
    if include_text_subtitle:
        # Text-only subtitle has no physical media ID; video source still does.
        timeline["tracks"].append({
            "id": "subtitle", "name": "Subtitles", "type": "subtitle",
            "clips": [{
                "id": "subtitle-clip", "trackId": "subtitle", "materialId": "",
                "start": {"value": 0, "timescale": 24},
                "duration": {"value": 24, "timescale": 24},
                "sourceIn": {"value": 0, "timescale": 24},
                "text": "FAKE-only subtitle text",
            }],
        })
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


def test_render_artifact_export_rights_revocation_and_missing_provenance(api_context):
    """FAKE-only: direct artifact download must not bypass adopted EXPORT rules."""
    client, sessions = api_context
    project = create_project(client, "FAKE direct export rights fence")
    upload, _candidate = _complete_render_candidate(client, project)
    db = sessions()
    try:
        task = db.query(DBRenderTask).filter(DBRenderTask.project_id == project["id"]).one()
        task_id = task.id
    finally:
        db.close()

    url = f"/renders/{task_id}/artifact"
    missing = client.get(url)
    assert missing.status_code == 403
    assert missing.json()["detail"]["failures"][0]["code"] == "RIGHTS_MISSING"

    now = datetime.datetime.now(datetime.timezone.utc)
    source = f"/projects/{project['id']}/asset-versions/{upload['assetVersion']['id']}/rights-snapshots"
    granted = client.post(source, json={
        "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
        "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
        "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
        "evidenceRef": "evidence://fake-direct-export-grant",
    })
    assert granted.status_code == 201, granted.text
    assert client.get(url).content == b"media"

    revoked = client.post(source, json={
        "status": "REVOKED", "purpose": "EXPORT", "territory": "GLOBAL",
        "evidenceRef": "evidence://fake-export-revocation",
    })
    assert revoked.status_code == 201, revoked.text
    denied = client.get(url, headers={"Range": "bytes=0-2"})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "RIGHTS_CHECK_FAILED"

    # A malformed durable render payload must not bypass provenance checking.
    db = sessions()
    try:
        task = db.query(DBRenderTask).filter(DBRenderTask.id == task_id).one()
        task.render_payload = {"canonicalTimeline": {"tracks": []}}
        db.commit()
    finally:
        db.close()
    malformed = client.get(url)
    assert malformed.status_code == 403
    assert malformed.json()["detail"]["failures"][0]["code"] == "RENDER_SOURCE_PROVENANCE_MISSING"



def test_render_artifact_text_only_subtitle_keeps_media_export_gate(api_context):
    """A valid text-only subtitle must not be mistaken for missing media provenance."""
    client, sessions = api_context
    project = create_project(client, "FAKE subtitle and EXPORT gate")
    upload, _candidate = _complete_render_candidate(
        client, project, include_text_subtitle=True,
    )
    db = sessions()
    try:
        task = db.query(DBRenderTask).filter(DBRenderTask.project_id == project["id"]).one()
        task_id = task.id
        tracks = task.render_payload["canonicalTimeline"]["tracks"]
        assert tracks[1]["type"] == "subtitle"
        assert tracks[1]["clips"][0]["materialId"] == ""
    finally:
        db.close()

    url = f"/renders/{task_id}/artifact"
    assert client.get(url).status_code == 403
    now = datetime.datetime.now(datetime.timezone.utc)
    rights_url = (
        f"/projects/{project['id']}/asset-versions/"
        f"{upload['assetVersion']['id']}/rights-snapshots"
    )
    grant = client.post(rights_url, json={
        "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
        "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
        "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
        "evidenceRef": "evidence://synthetic-subtitle-export-grant",
    })
    assert grant.status_code == 201, grant.text
    assert client.get(url).content == b"media"
    assert client.get(url, headers={"Range": "bytes=0-2"}).content == b"med"
    revoke = client.post(rights_url, json={
        "status": "REVOKED", "purpose": "EXPORT", "territory": "GLOBAL",
        "evidenceRef": "evidence://synthetic-subtitle-revoked",
    })
    assert revoke.status_code == 201
    assert client.get(url).status_code == 403


def test_upload_image_creates_image_asset_without_fake_video_duration(api_context):
    client, _ = api_context
    project = create_project(client, "Talking-head image input")
    payload = b"test-image-bytes"
    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": "1"},
        files={"file": ("portrait.png", payload, "image/png")},
    )
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["material"]["type"] == "image"
    assert result["material"]["contentType"] == "image/png"
    assert result["material"]["duration"] is None
    assert result["assetVersion"]["mediaType"] == "image"
    assert result["assetVersion"]["sizeBytes"] == len(payload)
    assert result["assetVersion"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["project"]["materials"][0]["type"] == "image"


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


@pytest.mark.parametrize("bad_field,bad_value", [
    ("sizeBytes", True),
    ("sizeBytes", "17"),
    ("sizeBytes", 17.5),
    ("sizeBytes", {"bytes": 17}),
    ("sizeBytes", None),
    ("durationSeconds", True),
    ("durationSeconds", "1.0"),
    ("durationSeconds", float("nan")),
    ("durationSeconds", float("inf")),
    ("durationSeconds", {"seconds": 1}),
])
def test_g70_g71_media_upload_rejects_untrusted_sidecar_probe_and_retains_hold(
    api_context, monkeypatch, bad_field, bad_value,
):
    """Malformed FAKE Sidecar evidence must not persist an asset or refund unknown remote bytes."""
    client, sessions = api_context
    project = create_project(client, "Strict Sidecar probe")
    adapter = client.app.state.video_use
    original_upload = adapter.upload_media

    def poisoned_sidecar(*args, **kwargs):
        result = original_upload(*args, **kwargs)
        return {**result, "metadata": {**result["metadata"], bad_field: bad_value}}

    monkeypatch.setattr(adapter, "upload_media", poisoned_sidecar)
    with sessions() as db:
        baseline_bytes = db.query(DBTenant).one().used_storage_bytes

    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    updated = client.get(f"/projects/{project['id']}").json()
    assert updated["revision"] == project["revision"]
    assert updated["materials"] == []
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == baseline_bytes + len(b"real-media-bytes")
        assert db.query(DBAssetVersion).count() == 0


@pytest.mark.parametrize("bad_response", [None, [], "not-a-mapping", {"metadata": []}])
def test_g70_g71_media_upload_rejects_malformed_sidecar_response_without_asset(
    api_context, monkeypatch, bad_response,
):
    """Reject an invalid Sidecar response root without speculative quota refund."""
    client, sessions = api_context
    project = create_project(client, "Strict Sidecar response")
    adapter = client.app.state.video_use
    monkeypatch.setattr(adapter, "upload_media", lambda *a, **kw: bad_response)
    with sessions() as db:
        baseline_bytes = db.query(DBTenant).one().used_storage_bytes

    response = client.post(
        f"/projects/{project['id']}/media",
        data={"expectedRevision": str(project["revision"])},
        files={"file": ("source.mp4", b"real-media-bytes", "video/mp4")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED"
    updated = client.get(f"/projects/{project['id']}").json()
    assert updated["revision"] == project["revision"]
    assert updated["materials"] == []
    with sessions() as db:
        assert db.query(DBTenant).one().used_storage_bytes == baseline_bytes + len(b"real-media-bytes")
        assert db.query(DBAssetVersion).count() == 0

@pytest.mark.parametrize("failure_kind", ["timeout_after_dispatch", "sidecar_503"])
@pytest.mark.parametrize(
    ("submit_method", "expected_path"),
    [
        ("submit_render", "/renders"),
        ("submit_transcription", "/transcriptions"),
        ("submit_timeline_view", "/timeline-views"),
    ],
)
def test_g70_g71_sidecar_mutating_job_submission_fails_closed_on_lost_ack(
    monkeypatch, failure_kind, submit_method, expected_path,
):
    """An ambiguous accepted Sidecar job must not be replayed as new work.

    In particular /transcriptions can trigger a paid upstream call. FAKE-only
    isolated HTTPX transport: no real Sidecar/Provider or billing calls.
    """
    import httpx
    from app.video_use_adapter import (
        VideoUseAdapter, VideoUseConnectionError, VideoUseTimeoutError,
    )

    outbound = []

    def fake_request(client, method, url, **kwargs):
        outbound.append((method, url, kwargs.get("json")))
        request = httpx.Request(method, url)
        if failure_kind == "timeout_after_dispatch":
            raise httpx.ReadTimeout("synthetic Sidecar lost ACK", request=request)
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    adapter = VideoUseAdapter(
        api_url="http://fake-sidecar.invalid", max_retries=2, backoff_factor=0,
    )
    with pytest.raises((VideoUseTimeoutError, VideoUseConnectionError)):
        getattr(adapter, submit_method)({"projectId": "fake-project"})

    assert len(outbound) == 1, "ambiguous mutating POST must not retry"
    assert outbound[0] == (
        "POST", f"http://fake-sidecar.invalid{expected_path}",
        {"projectId": "fake-project"},
    )


@pytest.mark.parametrize("failure_kind", ["timeout_after_dispatch", "sidecar_503"])
@pytest.mark.parametrize("with_idempotency_headers", [False, True])
def test_g70_g71_media_adapter_does_not_replay_ambiguous_upload_dispatch(monkeypatch, failure_kind, with_idempotency_headers):
    """FAKE-only transport test: an uncertain Sidecar POST must never be auto-replayed.

    A timeout or 503 may occur after remote bytes were committed. Neither
    auto-retry nor a guessed refund is permitted without a trusted receipt.
    This test makes zero real network/Provider calls.
    """
    import io
    import httpx
    from app.video_use_adapter import (
        VideoUseAdapter, VideoUseConnectionError, VideoUseTimeoutError,
    )

    outbound = []

    def fake_request(client, method, url, **kwargs):
        outbound.append((method, url, kwargs.get("headers")))
        request = httpx.Request(method, url)
        if failure_kind == "timeout_after_dispatch":
            raise httpx.ReadTimeout("simulated ambiguous remote commit", request=request)
        return httpx.Response(503, request=request)

    monkeypatch.setattr(httpx.Client, "request", fake_request)
    adapter = VideoUseAdapter(
        api_url="http://fake-sidecar.invalid", max_retries=2, backoff_factor=0,
    )
    payload = b"fake-only-uncertain-media-commit"
    binding = (
        {"idempotency_key": "fake-only-upload-1",
         "content_sha256": hashlib.sha256(payload).hexdigest()}
        if with_idempotency_headers else {}
    )
    with pytest.raises((VideoUseTimeoutError, VideoUseConnectionError)):
        adapter.upload_media(
            "fake-project", "clip.mp4", "video/mp4",
            io.BytesIO(payload), **binding,
        )

    assert len(outbound) == 1, "ambiguous upload must have exactly one dispatch"
    assert outbound[0][0] == "POST"
    assert outbound[0][1].endswith("/media")
    if with_idempotency_headers:
        assert outbound[0][2]["Idempotency-Key"] == "fake-only-upload-1"


@pytest.mark.parametrize("path", [
    "/jobs/../../health?exfil=1/artifact",
    "/jobs/%2e%2e%2fhealth/artifact",
    "/jobs/job-1?route=capabilities/artifact",
    "/jobs/job-1#fragment/artifact",
    "/media/project-a/../../health",
    "/media/project-a/media-1?route=/jobs",
    "/jobs//artifact",
])
def test_video_use_stream_rejects_malformed_sidecar_routes(monkeypatch, path):
    """FAKE-only: reject a poisoned persisted Sidecar ID before HTTP transport.

    No real Provider/Sidecar call or protected storage read. The Sidecar itself
    already validates identifiers; the authenticated API proxy must not rely on
    downstream URL parsing to preserve the intended resource scope.
    """
    import httpx
    from app.video_use_adapter import VideoUseAdapter, VideoUseError

    def forbidden_client(*args, **kwargs):
        raise AssertionError("invalid proxy route must not create an HTTP client")

    monkeypatch.setattr(httpx, "Client", forbidden_client)
    with pytest.raises(VideoUseError, match="invalid identifiers"):
        with VideoUseAdapter().stream(path):
            pytest.fail("malformed Sidecar path unexpectedly accepted")


@pytest.mark.parametrize("path", [
    "/jobs/11111111-1111-1111-1111-111111111111/artifact",
    "/media/11111111-1111-1111-1111-111111111111/media-1",
])
def test_video_use_stream_valid_sidecar_routes_preserve_range(monkeypatch, path):
    """FAKE HTTPX transport proves valid native routes are not regressed."""
    import httpx
    from app.video_use_adapter import VideoUseAdapter

    seen = []
    transport = httpx.MockTransport(
        lambda request: (
            seen.append((request.url.path, request.headers.get("range")))
            or httpx.Response(206, headers={"Content-Type": "video/mp4"}, content=b"abc")
        )
    )
    real_client = httpx.Client

    def mocked_client(*args, **kwargs):
        return real_client(*args, transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    with VideoUseAdapter().stream(path, headers={"Range": "bytes=0-2"}) as response:
        assert response.status_code == 206
        assert b"".join(response.iter_bytes()) == b"abc"
    assert seen == [(path, "bytes=0-2")]


def test_g70_g71_render_worker_lost_ack_lease_is_never_auto_redispatched(api_context):
    """FAKE-only native API DB: no durable upstream receipt means quarantine."""
    client, sessions = api_context
    project = create_project(client, "FAKE render lost ACK")
    now = datetime.datetime.now(datetime.timezone.utc)
    db = sessions()
    try:
        owner = db.query(DBUser).filter(DBUser.email == "owner@example.com").one()
        base = dict(
            tenant_id=owner.tenant_id, project_id=project["id"],
            requested_by=owner.id, status="RUNNING", progress=5,
            message="FAKE Worker submitted; ACK unknown", engine="video-use",
            render_payload={"requestId": "fake-only-lost-ack"},
            attempts=1, max_attempts=3, reserved_seconds=0,
            lease_owner="dead-worker", lease_expires_at=now - datetime.timedelta(minutes=1),
            created_at=now - datetime.timedelta(minutes=2), updated_at=now,
        )
        db.add(DBRenderTask(id="fake-only-lost-ack", upstream_job_id=None, **base))
        db.commit()
    finally:
        db.close()

    headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "another-worker"}
    # Claim must not bypass /recover and re-submit a historical RUNNING row.
    assert client.post("/internal/render-tasks/claim", headers=headers).status_code == 204
    recovered = client.post("/internal/render-tasks/recover", headers=headers)
    assert recovered.status_code == 200
    assert "fake-only-lost-ack" in recovered.json()["recoveredTaskIds"]
    assert client.post("/internal/render-tasks/claim", headers=headers).status_code == 204
    db = sessions()
    try:
        held = db.query(DBRenderTask).filter(DBRenderTask.id == "fake-only-lost-ack").one()
        assert canonical_task_status(held.status) == "UNKNOWN"
        assert held.attempts == 1
        assert held.upstream_job_id is None
        assert held.lease_owner is None and held.lease_expires_at is None
        assert held.completed_at is not None
    finally:
        db.close()


def test_g70_g71_render_retryable_worker_failure_becomes_unknown_not_queued(api_context):
    """FAKE-only: retryable is not independently trusted rejection evidence."""
    client, sessions = api_context
    project = create_project(client, "FAKE render retry fence")
    now = datetime.datetime.now(datetime.timezone.utc)
    db = sessions()
    try:
        owner = db.query(DBUser).filter(DBUser.email == "owner@example.com").one()
        db.add(DBRenderTask(
            id="fake-only-worker-retry", tenant_id=owner.tenant_id,
            project_id=project["id"], requested_by=owner.id, status="RUNNING",
            progress=10, message="FAKE Sidecar maybe accepted",
            engine="video-use", render_payload={"requestId": "fake-only-worker-retry"},
            upstream_job_id="fake-only-upstream-id", attempts=1, max_attempts=3,
            reserved_seconds=0, lease_owner="synthetic-worker",
            lease_expires_at=now + datetime.timedelta(minutes=1),
            created_at=now, updated_at=now,
        ))
        db.commit()
    finally:
        db.close()

    headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "synthetic-worker"}
    result = client.post(
        "/internal/render-tasks/fake-only-worker-retry/update",
        headers=headers,
        json={"status": "FAILED", "progress": 100,
              "message": "FAKE ambiguous transport", "retryable": True},
    )
    assert result.status_code == 200, result.text
    assert result.json()["canonicalStatus"] == "UNKNOWN"
    assert client.post("/internal/render-tasks/claim", headers=headers).status_code == 204
    db = sessions()
    try:
        task = db.query(DBRenderTask).filter(DBRenderTask.id == "fake-only-worker-retry").one()
        assert canonical_task_status(task.status) == "UNKNOWN"
        assert task.upstream_job_id == "fake-only-upstream-id"
        assert task.attempts == 1
        assert task.lease_owner is None and task.lease_expires_at is None
    finally:
        db.close()


def test_g70_g71_worker_update_rejects_expired_lease_and_upstream_id_rebind(api_context):
    """FAKE-only DB: stale callbacks and changed Sidecar IDs cannot alter tasks."""
    client, sessions = api_context
    project = create_project(client, "FAKE Worker lease and immutable receipt")
    now = datetime.datetime.now(datetime.timezone.utc)
    db = sessions()
    try:
        owner = db.query(DBUser).filter(DBUser.email == "owner@example.com").one()
        common = dict(
            tenant_id=owner.tenant_id, project_id=project["id"],
            requested_by=owner.id, status="RUNNING", progress=10,
            message="FAKE render leased", engine="video-use",
            render_payload={"requestId": "fake-only-lease-fence"},
            attempts=1, max_attempts=3,
            reserved_seconds=0, lease_owner="fake-worker",
            created_at=now, updated_at=now,
        )
        db.add(DBRenderTask(
            id="fake-expired-lease", lease_expires_at=now - datetime.timedelta(seconds=10),
            upstream_job_id="fake-original-expired-job",
            **common,
        ))
        db.add(DBRenderTask(
            id="fake-live-lease", lease_expires_at=now + datetime.timedelta(minutes=5),
            upstream_job_id="fake-original-live-job",
            **common,
        ))
        db.commit()
    finally:
        db.close()

    headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "fake-worker"}
    attempted_update = {
        "status": "SUCCEEDED", "progress": 100, "message": "stale completed",
        "upstreamJobId": "fake-other-job",
    }
    expired = client.post(
        "/internal/render-tasks/fake-expired-lease/update",
        headers=headers, json=attempted_update,
    )
    assert expired.status_code == 409, expired.text
    assert expired.json()["detail"]["code"] == "LEASE_EXPIRED"

    rebind = client.post(
        "/internal/render-tasks/fake-live-lease/update",
        headers=headers, json=attempted_update,
    )
    assert rebind.status_code == 409, rebind.text
    assert rebind.json()["detail"]["code"] == "UPSTREAM_JOB_ID_CONFLICT"

    db = sessions()
    try:
        for task_id in ("fake-expired-lease", "fake-live-lease"):
            task = db.query(DBRenderTask).filter(DBRenderTask.id == task_id).one()
            assert canonical_task_status(task.status) == "RUNNING"
            assert task.upstream_job_id == {
                "fake-expired-lease": "fake-original-expired-job",
                "fake-live-lease": "fake-original-live-job",
            }[task_id]
            assert task.progress == 10
            assert task.lease_owner == "fake-worker"
            assert task.completed_at is None
    finally:
        db.close()

    # An unexpired Worker with the *same* durable upstream ID can still poll.
    valid = client.post(
        "/internal/render-tasks/fake-live-lease/update",
        headers=headers,
        json={
            "status": "RUNNING", "progress": 45, "message": "poll only",
            "upstreamJobId": "fake-original-live-job",
        },
    )
    assert valid.status_code == 200, valid.text
    assert valid.json()["canonicalStatus"] == "RUNNING"


def test_g70_g71_recover_only_expired_leases_preserves_receipts(api_context):
    """FAKE-only: recovery holds unacknowledged tasks, resumes only known IDs."""
    client, sessions = api_context
    project = create_project(client, "FAKE selective recovery fence")
    now = datetime.datetime.now(datetime.timezone.utc)
    db = sessions()
    try:
        owner = db.query(DBUser).filter(DBUser.email == "owner@example.com").one()
        common = dict(
            tenant_id=owner.tenant_id, project_id=project["id"],
            requested_by=owner.id, status="RUNNING", progress=12,
            message="FAKE job", engine="video-use",
            render_payload={"requestId": "fake-only-recovery-lock"},
            attempts=1, max_attempts=3, reserved_seconds=0,
            lease_owner="old-worker", created_at=now, updated_at=now,
        )
        db.add(DBRenderTask(
            id="fake-recover-no-receipt", upstream_job_id=None,
            lease_expires_at=now - datetime.timedelta(seconds=30), **common,
        ))
        db.add(DBRenderTask(
            id="fake-recover-has-receipt", upstream_job_id="fake-durable-job",
            lease_expires_at=now - datetime.timedelta(seconds=30), **common,
        ))
        db.add(DBRenderTask(
            id="fake-recover-fresh", upstream_job_id="fake-fresh-job",
            lease_expires_at=now + datetime.timedelta(minutes=5), **common,
        ))
        db.commit()
    finally:
        db.close()

    headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "new-worker"}
    recovered = client.post("/internal/render-tasks/recover", headers=headers)
    assert recovered.status_code == 200, recovered.text
    assert set(recovered.json()["recoveredTaskIds"]) == {
        "fake-recover-no-receipt", "fake-recover-has-receipt",
    }
    again = client.post("/internal/render-tasks/recover", headers=headers)
    assert again.status_code == 200 and again.json()["recoveredTaskIds"] == []

    db = sessions()
    try:
        unacked = db.get(DBRenderTask, "fake-recover-no-receipt")
        assert canonical_task_status(unacked.status) == "UNKNOWN"
        assert unacked.upstream_job_id is None
        assert unacked.lease_owner is None and unacked.lease_expires_at is None
        assert unacked.completed_at is not None
        known = db.get(DBRenderTask, "fake-recover-has-receipt")
        assert canonical_task_status(known.status) == "RUNNING"
        assert known.upstream_job_id == "fake-durable-job"
        assert known.attempts == 1
        assert known.lease_owner is None and known.lease_expires_at is None
        fresh = db.get(DBRenderTask, "fake-recover-fresh")
        assert canonical_task_status(fresh.status) == "RUNNING"
        assert fresh.lease_owner == "old-worker"
        assert fresh.lease_expires_at is not None
        assert fresh.upstream_job_id == "fake-fresh-job"
    finally:
        db.close()

    # The recovered known-ID row can only resume polling; no new submission.
    resumed = client.post("/internal/render-tasks/claim", headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["taskId"] == "fake-recover-has-receipt"
    assert resumed.json()["upstreamJobId"] == "fake-durable-job"
    assert resumed.json()["attempts"] == 1
    assert client.post("/internal/render-tasks/claim", headers=headers).status_code == 204
