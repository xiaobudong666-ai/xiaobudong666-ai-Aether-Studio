import secrets
import inspect
from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.database import Base, build_engine
from app.main import create_app
from app.moneyprinter_adapter import (
    MoneyPrinterConnectionError,
    MoneyPrinterTurboAdapter,
)
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

OWNER_PASSWORD = secrets.token_urlsafe(24)


@pytest.fixture()
def mpt_client(tmp_path):
    test_engine = build_engine(f"sqlite:///{tmp_path / 'moneyprinter.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def get_test_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    test_app = create_app(
        app_engine=test_engine,
        db_dependency=get_test_db,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        cookie_secure=False,
        enforce_csrf=True,
    )
    test_app.state.moneyprinter = MagicMock()
    with TestClient(test_app) as client:
        assert client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": OWNER_PASSWORD},
        ).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, test_app.state.moneyprinter
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()

@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/moneyprinter/health"),
        ("get", "/moneyprinter/capabilities"),
        ("post", "/moneyprinter/generate"),
        ("get", "/moneyprinter/status/mpt-task-789"),
    ],
)
def test_im16_17_api_moneyprinter_legacy_routes_are_retired(mpt_client, method, path):
    client, mock_adapter = mpt_client
    response = client.post(path, json={}) if method == "post" else client.get(path)
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "LEGACY_PROVIDER_ROUTE_RETIRED"
    mock_adapter.assert_not_called()


def test_im16_18_api_runtime_has_no_provider_adapter_call_path():
    import app.main as api_main

    source = inspect.getsource(api_main)
    assert "from .moneyprinter_adapter import" not in source
    assert "state.moneyprinter" not in source
    assert "LEGACY_PROVIDER_ROUTE_RETIRED" in source


# --- Unit Tests for actual MoneyPrinterTurboAdapter Class in apps/api ---

def test_api_adapter_initialization():
    adapter = MoneyPrinterTurboAdapter(
        api_url="http://mock-mpt-api:8080",
        timeout=3.0,
        max_retries=1,
        backoff_factor=0.1,
        degrade_on_failure=True
    )
    assert adapter.api_url == "http://mock-mpt-api:8080"
    assert adapter.timeout == 3.0
    assert adapter.max_retries == 1
    assert adapter.degrade_on_failure is True

@patch("httpx.Client")
def test_api_adapter_check_health_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client.get.return_value = mock_response

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1)
    health = adapter.check_health()
    assert health["status"] == "healthy"
    assert health["responsive"] is True
    mock_client.get.assert_called_once_with("http://mock-mpt:8080/openapi.json", params=None)

@patch("httpx.Client")
def test_api_adapter_check_health_failure_on_404_no_longer_healthy(mock_client_class):
    # Strict test: 404/HTTP Error must NOT be treated as healthy
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
def test_api_adapter_generate_video_failure_no_forgery(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.post.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)
    with pytest.raises(MoneyPrinterConnectionError):
        adapter.generate_video(subject="cats")

@patch("httpx.Client")
def test_api_adapter_get_task_status_failure_with_degrade_returns_failed_no_forged_success(mock_client_class):
    mock_client = MagicMock()
    mock_client_class.return_value.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Connection refused")

    adapter = MoneyPrinterTurboAdapter(api_url="http://mock-mpt:8080", max_retries=1, degrade_on_failure=True)
    status_res = adapter.get_task_status("test-task")

    assert status_res["status"] == "failed"
    assert status_res["progress"] == 0
    assert "video_url" not in status_res
