import io
import logging
from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.moneyprinter_adapter import (
    ADAPTER_VERSION,
    UPSTREAM_PIN,
    MoneyPrinterAmbiguousSubmissionError,
    MoneyPrinterArtifactError,
    MoneyPrinterConnectionError,
    MoneyPrinterError,
    MoneyPrinterTimeoutError,
    MoneyPrinterTurboAdapter,
)


def adapter_with(handler, **kwargs):
    adapter = MoneyPrinterTurboAdapter(
        api_url="http://moneyprinter-sidecar:8080",
        timeout=1,
        max_retries=kwargs.pop("max_retries", 1),
        backoff_factor=0,
        **kwargs,
    )
    transport = httpx.MockTransport(handler)
    adapter._client = lambda: httpx.Client(  # noqa: SLF001 - contract test seam
        transport=transport, trust_env=False, follow_redirects=False, timeout=1
    )
    return adapter


def json_response(request, payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers, request=request)


def test_adapter_initialization_is_pinned_and_non_secret():
    adapter = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    assert adapter.api_url == "http://moneyprinter-sidecar:8080"
    assert ADAPTER_VERSION == "aether-moneyprinter-v2"
    assert UPSTREAM_PIN == "475f21147f0808f5ffe3f58af9ab794b28a4da2c"


def test_health_and_capabilities_are_sanitized():
    adapter = adapter_with(lambda request: json_response(request, {}))
    health = adapter.check_health()
    capabilities = adapter.get_capabilities()
    serialized = str({"health": health, "capabilities": capabilities}).lower()
    assert health["status"] == "healthy"
    assert capabilities["capabilities"]["artifactStreaming"] is True
    assert "http://" not in serialized
    assert "api_key" not in serialized


def test_connection_and_read_timeout_have_stable_codes():
    def connection_failure(request):
        raise httpx.ConnectError("private host detail", request=request)

    with pytest.raises(MoneyPrinterConnectionError) as connection:
        adapter_with(connection_failure).get_task_status("task-1")
    assert connection.value.code == "PROVIDER_CONNECTION_FAILED"

    def read_failure(request):
        raise httpx.ReadTimeout("private response detail", request=request)

    with pytest.raises(MoneyPrinterTimeoutError) as timeout:
        adapter_with(read_failure).get_task_status("task-1")
    assert timeout.value.code == "PROVIDER_READ_TIMEOUT"


def test_im16_20_submit_payload_is_allowlisted():
    captured = {}

    def handler(request):
        captured.update(__import__("json").loads(request.content))
        return json_response(request, {"data": {"task_id": "task-1"}})

    adapter = adapter_with(handler)
    assert adapter.generate_video("safe prompt") == "task-1"
    assert set(captured) == {
        "video_subject", "video_aspect", "voice_name",
        "video_concat_mode", "video_clip_duration",
    }
    with pytest.raises(TypeError):
        adapter.generate_video("x", model="arbitrary")


def test_im16_21_4xx_is_not_retried_but_429_and_5xx_are_bounded():
    calls = []

    def reject_4xx(request):
        calls.append(request)
        return json_response(request, {"private": "body"}, status=400)

    with pytest.raises(MoneyPrinterError) as rejected:
        adapter_with(reject_4xx, max_retries=3).generate_video("safe")
    assert rejected.value.code == "PROVIDER_4XX"
    assert len(calls) == 1

    calls.clear()

    def transient(request):
        calls.append(request)
        if len(calls) < 3:
            return json_response(request, {}, status=500)
        return json_response(request, {"data": {"task_id": "task-2"}})

    assert adapter_with(transient, max_retries=3).generate_video("safe") == "task-2"
    assert len(calls) == 3


def test_im16_22_ambiguous_post_is_never_replayed():
    calls = []

    def ambiguous(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    with pytest.raises(MoneyPrinterAmbiguousSubmissionError):
        adapter_with(ambiguous, max_retries=3).generate_video("safe")
    assert len(calls) == 1


def test_im16_24_unknown_upstream_state_stays_unknown():
    adapter = adapter_with(
        lambda request: json_response(request, {"data": {"state": 999, "progress": 100}})
    )
    status = adapter.get_task_status("task-1")
    assert status == {"task_id": "task-1", "status": "unknown", "progress": 100}


def test_im16_25_raw_body_prompt_and_sensitive_headers_are_not_logged(caplog):
    def reject(request):
        return json_response(request, {"api_key": "leak", "prompt": "private prompt"}, status=400)

    with caplog.at_level(logging.INFO), pytest.raises(MoneyPrinterError):
        adapter_with(reject).generate_video("private prompt")
    serialized = caplog.text.lower()
    assert "private prompt" not in serialized
    assert "api_key" not in serialized
    assert "authorization" not in serialized


@pytest.mark.parametrize(
    "source",
    ["/artifacts/final.mp4", "http://moneyprinter-sidecar:8080/artifacts/final.mp4"],
)
def test_im16_26_same_origin_relative_or_absolute_artifact_is_accepted(source):
    adapter = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    assert adapter._validated_artifact_url(source) == "http://moneyprinter-sidecar:8080/artifacts/final.mp4"  # noqa: SLF001


@pytest.mark.parametrize(
    "source",
    [
        "https://evil.example/artifacts/a.mp4",
        "http://moneyprinter-sidecar:9999/artifacts/a.mp4",
        "http://user:pass@moneyprinter-sidecar:8080/artifacts/a.mp4",
        "file:///artifacts/a.mp4",
        "/artifacts/../secret.mp4",
        "/tmp/a.mp4",
        "/artifacts/a.mp4?token=secret",
    ],
)
def test_im16_27_external_origin_credentials_protocol_and_traversal_are_rejected(source):
    adapter = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    with pytest.raises(MoneyPrinterArtifactError):
        adapter._validated_artifact_url(source)  # noqa: SLF001


def test_im16_28_redirect_is_rejected_without_following():
    adapter = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    adapter._artifact_sources["artifact-1"] = "/artifacts/final.mp4"  # noqa: SLF001
    adapter._client = lambda: httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://evil.example/a.mp4"}, request=request
            )
        ),
        trust_env=False,
        follow_redirects=False,
    )
    with pytest.raises(MoneyPrinterArtifactError) as rejected:
        adapter.stream_artifact("artifact-1")
    assert rejected.value.code == "ARTIFACT_REDIRECT_REJECTED"


@patch("httpx.Client")
def test_im16_29_http_client_disables_proxy_environment(mock_client):
    client = MagicMock()
    mock_client.return_value.__enter__.return_value = client
    client.request.return_value = MagicMock(status_code=200)
    MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080").check_health()
    assert mock_client.call_args.kwargs["trust_env"] is False
    assert mock_client.call_args.kwargs["follow_redirects"] is False


@pytest.mark.parametrize(
    ("headers", "chunks", "code"),
    [
        ({"content-type": "text/plain", "content-length": "1"}, [b"x"], "ARTIFACT_CONTENT_TYPE_INVALID"),
        ({"content-type": "video/mp4", "content-length": "11"}, [b"x"], "ARTIFACT_TOO_LARGE"),
        ({"content-type": "video/mp4"}, [], "ARTIFACT_EMPTY"),
        ({"content-type": "video/mp4"}, [b"123456", b"123456"], "ARTIFACT_TOO_LARGE"),
    ],
)
def test_im16_30_invalid_or_oversize_stream_never_returns_bytes(headers, chunks, code):
    def handler(request):
        return httpx.Response(200, headers=headers, content=b"".join(chunks), request=request)

    adapter = adapter_with(handler, max_artifact_bytes=10)
    adapter._artifact_sources["artifact-1"] = "/artifacts/final.mp4"  # noqa: SLF001
    with pytest.raises(MoneyPrinterArtifactError) as rejected:
        adapter.stream_artifact("artifact-1")
    assert rejected.value.code == code


def test_valid_stream_is_read_only_and_linked_to_artifact_identifier():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "video/mp4", "content-length": "5"},
            content=b"video",
            request=request,
        )

    adapter = adapter_with(handler, max_artifact_bytes=10)
    adapter._artifact_sources["artifact-1"] = "/artifacts/final.mp4"  # noqa: SLF001
    stream = adapter.stream_artifact("artifact-1")
    assert isinstance(stream, io.IOBase)
    assert stream.read() == b"video"


def test_im16_32_cancel_is_local_when_upstream_capability_is_absent():
    adapter = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    with pytest.raises(MoneyPrinterError) as unsupported:
        adapter.cancel_task("task-1")
    assert unsupported.value.code == "PROVIDER_CANCEL_UNSUPPORTED"
