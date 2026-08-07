from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.main import apply_project_update, create_app
from app.schemas import UpdateProjectRequest


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
    def stream(self, _path):
        class Response:
            def iter_bytes(self):
                yield b"media"

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
    )
    with TestClient(test_app) as client:
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
    assert render_response.status_code == 200
    task_id = render_response.json()["taskId"]
    assert render_response.json()["mock"] is False

    first_snapshot = client.get("/events?once=true")
    assert first_snapshot.status_code == 200
    assert "event: task_progress" in first_snapshot.text
    assert task_id in first_snapshot.text

    completed_snapshot = client.get("/events?once=true")
    assert f'"taskId":"{task_id}"' in completed_snapshot.text
    assert '"status":"completed"' in completed_snapshot.text
