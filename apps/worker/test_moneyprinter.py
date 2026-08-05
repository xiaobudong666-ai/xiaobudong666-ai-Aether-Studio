import pytest
from unittest.mock import MagicMock, patch, ANY
import httpx
from app.moneyprinter_adapter import (
    MoneyPrinterTurboAdapter,
    MoneyPrinterError,
    MoneyPrinterTimeoutError,
    MoneyPrinterConnectionError,
    MoneyPrinterTaskFailedError,
)

def test_adapter_initialization():
    adapter = MoneyPrinterTurboAdapter(
        api_url="http://mock-mpt:8080",
        timeout=5.0,
        max_retries=2,
        backoff_factor=0.1,
        degrade_on_failure=False
    )
    assert adapter.api_url == "http://mock-mpt:8080"
    assert adapter.timeout == 5.0
    assert adapter.max_retries == 2
    assert adapter.backoff_factor == 0.1
    assert adapter.degrade_on_failure is False

@patch("httpx.Client")
def test_check_health_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    health = adapter.check_health()

    assert health["status"] == "healthy"
    assert health["responsive"] is True
    assert health["url"] == "http://mock-mpt:8080"
    mock_client.get.assert_called_once_with("http://mock-mpt:8080/openapi.json", params=None)

@patch("httpx.Client")
def test_check_health_failure_no_degrade(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=False)
    health = adapter.check_health()

    assert health["status"] == "unhealthy"
    assert health["responsive"] is False
    assert "Connection refused" in health["error"]

@patch("httpx.Client")
def test_check_health_failure_on_404_no_longer_healthy(mock_client_class):
    # Strict negative check: 404/HTTP Error must NOT be treated as healthy
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=MagicMock(), response=mock_response
    )
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=False)
    health = adapter.check_health()
    assert health["status"] == "unhealthy"
    assert health["responsive"] is False

@patch("httpx.Client")
def test_check_health_failure_with_degrade(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)
    health = adapter.check_health()

    assert health["status"] == "degraded"
    assert health["responsive"] is False
    assert health["fallback_active"] is True
    assert health["capabilities"]["video_generation"] == "unavailable"

@patch("httpx.Client")
def test_get_capabilities_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    caps = adapter.get_capabilities()

    assert caps["status"] == "active"
    assert caps["capabilities"]["video_generation"] == "unknown (adapter integrated, credentials not configured)"
    assert caps["capabilities"]["subtitles_sync"] == "unavailable"
    assert caps["capabilities"]["tts_voiceover"] == "unavailable"
    assert caps["pinned_upstream"]["version"] == "v1.2.7"
    assert caps["pinned_upstream"]["commit"] == "475f21147f0808f5ffe3f58af9ab794b28a4da2c"

@patch("httpx.Client")
def test_generate_video_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    # Aligned response packaging: {"status": 200, "data": {"task_id": "test-task-123"}}
    mock_response.json.return_value = {"status": 200, "data": {"task_id": "test-task-123"}}
    mock_client.post.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    task_id = adapter.generate_video(subject="cats")

    assert task_id == "test-task-123"
    mock_client.post.assert_called_once_with("http://mock-mpt:8080/api/v1/videos", json=ANY, params=None)

@patch("httpx.Client")
def test_generate_video_failure_no_forgery(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)

    # Positive negative test: When Sidecar is disconnected, we MUST NOT fake task ID, but strictly raise.
    with pytest.raises(MoneyPrinterConnectionError):
        adapter.generate_video(subject="cats")

@patch("httpx.Client")
def test_get_task_status_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    # Aligned upstream v1.2.7 response packaging using state integer (1: completed)
    mock_response.json.return_value = {
        "status": 200,
        "data": {
            "state": 1,
            "progress": 100,
            "combined_videos": ["http://foo.mp4"]
        }
    }
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    status_data = adapter.get_task_status("test-task-123")

    # Aligned and mapped standard statuses
    assert status_data["status"] == "completed"
    assert status_data["progress"] == 100
    assert status_data["video_url"] == "http://foo.mp4"
    mock_client.get.assert_called_once_with("http://mock-mpt:8080/api/v1/tasks/test-task-123", params=None)

@patch("httpx.Client")
def test_get_task_status_failed_raises_custom_error(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    # Aligned response packaging using state integer (-1: failed)
    mock_response.json.return_value = {
        "status": 200,
        "data": {
            "state": -1,
            "message": "out of disk"
        }
    }
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    with pytest.raises(MoneyPrinterTaskFailedError) as exc_info:
        adapter.get_task_status("test-task")
    assert "failed with state -1" in str(exc_info.value)

@patch("httpx.Client")
def test_get_task_status_failure_with_degrade_returns_failed_no_forged_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)
    status_res = adapter.get_task_status("test-task")

    # Positive negative test: When Sidecar is disconnected, status check must return failed with 0 progress and no fake URL
    assert status_res["status"] == "failed"
    assert status_res["progress"] == 0
    assert "degraded" in status_res
    assert "video_url" not in status_res

@patch("httpx.Client")
def test_exponential_backoff_retry_on_500(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    # First attempt: 500 error, Second attempt: 200 success
    mock_response_500 = MagicMock()
    mock_response_500.status_code = 500
    mock_response_500.text = "Internal Server Error"
    # Create raise_for_status mock
    mock_response_500.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500 Error", request=MagicMock(), response=mock_response_500
    )

    mock_response_200 = MagicMock()
    mock_response_200.status_code = 200
    mock_response_200.json.return_value = {
        "status": 200,
        "data": {
            "task_id": "success-task"
        }
    }

    mock_client.post.side_effect = [mock_response_500, mock_response_200]

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=2, backoff_factor=0.01)
    task_id = adapter.generate_video(subject="dogs")

    assert task_id == "success-task"
    assert mock_client.post.call_count == 2

@patch("httpx.Client")
def test_timeout_error_mapping(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ReadTimeout("Timeout reading")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=2, backoff_factor=0.01)
    with pytest.raises(MoneyPrinterTimeoutError):
        adapter.get_task_status("some-task")
