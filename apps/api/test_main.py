import os
import pytest

# Force isolated test SQLite database URL before imports are evaluated
os.environ["DATABASE_URL"] = "sqlite:///aether_test.db"

from fastapi.testclient import TestClient
from app.main import app, get_db, sse_events
from app.database import Base, engine, SessionLocal

@pytest.fixture(scope="module", autouse=True)
def setup_test_db():
    # Setup test schema
    Base.metadata.create_all(bind=engine)
    yield
    # Clean up test database file after tests complete
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("aether_test.db"):
        try:
            os.remove("aether_test.db")
        except OSError:
            pass

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c

def test_health_check_dynamic_wal(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "api"
    # Ensure it's querying actual journal mode
    assert "journal_mode" in data
    assert data["journal_mode"] in ["WAL", "MEMORY", "DELETE", "TRUNCATE"]

def test_project_crud_lifecycle_with_concurrency_locks(client):
    # 1. Create project
    response = client.post("/projects", json={"name": "Aether Epic Anime"})
    assert response.status_code == 201
    project = response.json()
    assert project["name"] == "Aether Epic Anime"
    assert project["revision"] == 1
    assert project["timeline"]["version"] == "1.1"

    project_id = project["id"]

    # 2. Get project details
    response = client.get(f"/projects/{project_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "Aether Epic Anime"

    # 3. Update project (Valid expectedRevision)
    update_payload = {
        "name": "Aether Stylized V2",
        "expectedRevision": 1
    }
    response = client.put(f"/projects/{project_id}", json=update_payload)
    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == "Aether Stylized V2"
    assert updated["revision"] == 2

    # 4. Trigger concurrency conflict (Expected is 1, but current is 2)
    conflict_payload = {
        "name": "Stale Update Blocked",
        "expectedRevision": 1
    }
    response = client.put(f"/projects/{project_id}", json=conflict_payload)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"

@pytest.mark.anyio
async def test_sse_generator():
    # Test the SSE generator directly to avoid blocking test execution client streams
    response = await sse_events()
    generator = response.body_iterator

    # Read first event produced by generator
    first_event = await generator.__anext__()
    assert "heartbeat" in first_event or "timestamp" in first_event
