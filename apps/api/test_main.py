import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.database import Base, build_engine
from app.main import apply_project_update, create_app
from app.schemas import UpdateProjectRequest


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

    render_response = client.post(f"/projects/{project['id']}/render")
    assert render_response.status_code == 200
    task_id = render_response.json()["taskId"]
    assert render_response.json()["mock"] is True

    first_snapshot = client.get("/events?once=true")
    assert first_snapshot.status_code == 200
    assert "event: task_progress" in first_snapshot.text
    assert task_id in first_snapshot.text

    time.sleep(0.08)
    completed_snapshot = client.get("/events?once=true")
    assert f'"taskId":"{task_id}"' in completed_snapshot.text
    assert '"status":"completed"' in completed_snapshot.text
