from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from app.main import create_app

@pytest.fixture()
def mpt_client():
    test_app = create_app()
    test_app.state.moneyprinter = MagicMock()
    with TestClient(test_app) as client:
        yield client, test_app.state.moneyprinter

def test_api_moneyprinter_health(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.check_health.return_value = {
        "status": "healthy",
        "service": "moneyprinter-sidecar",
        "responsive": True,
    }

    response = client.get("/moneyprinter/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    mock_adapter.check_health.assert_called_once()

def test_api_moneyprinter_capabilities(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.get_capabilities.return_value = {
        "status": "active",
        "capabilities": {
            "video_generation": True,
        }
    }

    response = client.get("/moneyprinter/capabilities")
    assert response.status_code == 200
    assert response.json()["capabilities"]["video_generation"] is True
    mock_adapter.get_capabilities.assert_called_once()

def test_api_moneyprinter_generate(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.generate_video.return_value = "mpt-task-789"

    payload = {
        "video_subject": "AI evolution",
        "video_aspect": "16:9",
        "voice_name": "en-US-JennyNeural"
    }
    response = client.post("/moneyprinter/generate", json=payload)
    assert response.status_code == 200
    assert response.json() == {"task_id": "mpt-task-789", "status": "submitted"}
    mock_adapter.generate_video.assert_called_once_with(
        subject="AI evolution",
        aspect="16:9",
        voice_name="en-US-JennyNeural",
        video_concat_mode="random",
        video_clip_duration=5,
    )

def test_api_moneyprinter_generate_failure(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.generate_video.side_effect = Exception("Connection lost")

    payload = {"video_subject": "AI evolution"}
    response = client.post("/moneyprinter/generate", json=payload)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MONEYPRINTER_API_ERROR"

def test_api_moneyprinter_status(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.get_task_status.return_value = {
        "task_id": "mpt-task-789",
        "status": "completed",
        "video_url": "http://localhost/out.mp4"
    }

    response = client.get("/moneyprinter/status/mpt-task-789")
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    mock_adapter.get_task_status.assert_called_once_with("mpt-task-789")

def test_api_moneyprinter_status_failure(mpt_client):
    client, mock_adapter = mpt_client
    mock_adapter.get_task_status.side_effect = Exception("API offline")

    response = client.get("/moneyprinter/status/mpt-task-789")
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "MONEYPRINTER_STATUS_ERROR"
