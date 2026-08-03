import pytest
from unittest.mock import MagicMock, patch
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

    # Mock /docs response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    health = adapter.check_health()

    assert health["status"] == "healthy"
    assert health["responsive"] is True
    assert health["url"] == "http://mock-mpt:8080"
    mock_client.get.assert_called_once_with("http://mock-mpt:8080/docs", params=None)

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
def test_check_health_failure_with_degrade(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)
    health = adapter.check_health()

    assert health["status"] == "degraded"
    assert health["responsive"] is False
    assert health["fallback_active"] is True

@patch("httpx.Client")
def test_generate_video_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"task_id": "test-task-123"}
    mock_client.post.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    task_id = adapter.generate_video(subject="cats")

    assert task_id == "test-task-123"
    mock_client.post.assert_called_once()

@patch("httpx.Client")
def test_get_task_status_failed_raises_custom_error(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "failed", "message": "out of disk"}
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    with pytest.raises(MoneyPrinterTaskFailedError) as exc_info:
        adapter.get_task_status("test-task")
    assert "out of disk" in str(exc_info.value)

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
    mock_response_200.json.return_value = {"task_id": "success-task"}

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
