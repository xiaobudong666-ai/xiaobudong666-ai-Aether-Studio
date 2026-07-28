import pytest
from fastapi.testclient import TestClient
from app.main import app, get_db
from app.database import Base, engine, SessionLocal

# Create a clean test database session
TestingSessionLocal = SessionLocal()

@pytest.fixture(scope="module")
def client():
    # Use the test client
    with TestClient(app) as c:
        yield c

def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "api"
    assert data["journal_mode"] == "WAL"

def test_project_lifecycle(client):
    # 1. List Projects (should be empty initially or have what's in database)
    response = client.get("/projects")
    assert response.status_code == 200
    initial_count = len(response.json())

    # 2. Create Project
    response = client.post("/projects", json={"name": "Test Project"})
    assert response.status_code == 201
    proj = response.json()
    assert proj["name"] == "Test Project"
    assert proj["revision"] == 1
    assert proj["timeline"]["version"] == "1.1"
    assert proj["materials"] == []
    proj_id = proj["id"]

    # 3. Get Project
    response = client.get(f"/projects/{proj_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "Test Project"

    # 4. Update Project (Success)
    update_data = {
        "name": "Updated Project Name",
        "timeline": {
            "version": "1.1",
            "tracks": [
                {
                    "id": "track-1",
                    "name": "Video Track 1",
                    "type": "video",
                    "clips": []
                }
            ]
        },
        "materials": [],
        "expectedRevision": 1
    }
    response = client.put(f"/projects/{proj_id}", json=update_data)
    assert response.status_code == 200
    updated_proj = response.json()
    assert updated_proj["name"] == "Updated Project Name"
    assert updated_proj["revision"] == 2
    assert len(updated_proj["timeline"]["tracks"]) == 1

    # 5. Update Project with Concurrency Conflict (Failure)
    conflict_data = {
        "name": "Conflicting Update",
        "expectedRevision": 1 # Expected is 1, but current in DB is 2
    }
    response = client.put(f"/projects/{proj_id}", json=conflict_data)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"

    # 6. Non-existent Project (404)
    response = client.get("/projects/non-existent-id")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "PROJECT_NOT_FOUND"
