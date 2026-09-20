"""A1-P1: HeyGen talking-head Provider adapter contract and fail-closed boundary.

All tests are fake-only: every request is served by ``httpx.MockTransport`` and
no real HeyGen request is ever issued.  They prove the adapter is default-off,
performs a single submission attempt with zero retry/fallback, enforces 9:16 /
<=10s / single output / cost ceiling, pins Provider identity, and returns only
a binary artifact stream for re-entry into Aether artifact intake.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest

from app.talking_head_provider_adapter import (
    ADAPTER_VERSION,
    HEYGEN_API_KEY_ENV_VAR,
    HEYGEN_API_ORIGIN,
    HEYGEN_ARTIFACT_ORIGINS,
    HEYGEN_MEDIA_ORIGIN,
    HeyGenTalkingHeadAdapter,
    TalkingHeadAmbiguousSubmissionError,
    TalkingHeadArtifactError,
    TalkingHeadCostCeilingError,
    TalkingHeadIdentityError,
    TalkingHeadJobSpec,
    TalkingHeadProviderDisabled,
    TalkingHeadProviderError,
)


def adapter_with(handler, **kwargs):
    transport = httpx.MockTransport(handler)
    return HeyGenTalkingHeadAdapter(enabled=True, transport=transport, **kwargs)


def json_response(request, payload, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers, request=request)


def valid_spec(**overrides):
    values = dict(
        text="大家好，这是一条十秒口播测试。",
        duration_seconds=10,
        aspect_ratio="9:16",
        resolution="1080p",
        voice_id="voice-zh-CN-1",
        image_url="https://static.example/photo.jpg",
    )
    values.update(overrides)
    return TalkingHeadJobSpec(**values)


def completed_payload(video_id="video-1", video_url="https://cdn.heygen.com/videos/video-1.mp4"):
    return {"data": {"video_id": video_id, "status": "completed", "video_url": video_url, "duration": 10}}


def test_adapter_is_default_disabled_and_never_reads_secret():
    adapter = HeyGenTalkingHeadAdapter()
    assert adapter.enabled is False
    assert adapter.check_health()["status"] == "disabled"
    with pytest.raises(TalkingHeadProviderDisabled) as disabled:
        adapter.submit(valid_spec())
    assert disabled.value.code == "PROVIDER_DISABLED"
    assert HEYGEN_API_KEY_ENV_VAR == "HEYGEN_API_KEY"
    assert "HEYGEN_API_KEY" in adapter.check_health()["credentialEnvVar"]


def test_enabled_origin_must_match_pinned_identity():
    with pytest.raises(TalkingHeadIdentityError):
        HeyGenTalkingHeadAdapter(enabled=True, api_url="https://evil.example")
    with pytest.raises(ValueError):
        HeyGenTalkingHeadAdapter(enabled=True, api_url="http://user:pass@api.heygen.com")
    assert HEYGEN_API_ORIGIN == ("https", "api.heygen.com", None)


def test_submit_is_single_allowlisted_9_16_1080p_call():
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return json_response(request, {"data": {"video_id": "video-1"}})

    adapter = adapter_with(handler)
    assert adapter.submit(valid_spec()) == "video-1"
    assert captured["url"] == "https://api.heygen.com/v3/videos"
    assert captured["payload"] == {
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "image_url": "https://static.example/photo.jpg",
        "script": "大家好，这是一条十秒口播测试。",
        "voice_id": "voice-zh-CN-1",
    }


def test_ambiguous_submission_is_never_replayed():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    with pytest.raises(TalkingHeadAmbiguousSubmissionError) as ambiguous:
        adapter_with(handler).submit(valid_spec())
    assert ambiguous.value.code == "AMBIGUOUS_SUBMISSION"
    assert len(calls) == 1


def test_provider_http_errors_fail_closed_without_retry():
    calls = []

    def reject(request):
        calls.append(request)
        return json_response(request, {"error": "private body"}, status=402)

    with pytest.raises(TalkingHeadProviderError) as err:
        adapter_with(reject).submit(valid_spec())
    assert err.value.code == "PROVIDER_BALANCE_INSUFFICIENT"
    assert len(calls) == 1


def test_cost_ceiling_is_checked_before_any_request():
    calls = []

    def handler(request):
        calls.append(request)
        return json_response(request, {"data": {"video_id": "video-1"}})

    adapter = adapter_with(handler, cost_ceiling_usd=0.50, cost_per_second_usd=0.10)
    with pytest.raises(TalkingHeadCostCeilingError):
        adapter.submit(valid_spec(duration_seconds=10))
    assert calls == []


def test_job_spec_rejects_wrong_aspect_duration_and_ambiguous_sources():
    with pytest.raises(TalkingHeadProviderError):
        valid_spec(aspect_ratio="16:9").validate()
    with pytest.raises(TalkingHeadProviderError):
        valid_spec(duration_seconds=11).validate()
    with pytest.raises(TalkingHeadProviderError):
        valid_spec(audio_url="https://static.example/voice.wav").validate()
    with pytest.raises(TalkingHeadProviderError):
        TalkingHeadJobSpec(text="x", duration_seconds=5, image_url="a", avatar_id="b").validate()


def test_status_completed_yields_single_artifact_reference():
    def handler(request):
        assert str(request.url) == "https://api.heygen.com/v3/videos/video-1"
        return json_response(request, completed_payload())

    adapter = adapter_with(handler)
    status = adapter.status("video-1")
    assert status["job_id"] == "video-1"
    assert status["status"] == "completed"
    assert status["providerArtifactId"] == "video-1"
    assert adapter._artifact_urls["video-1"] == "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001


def test_status_unknown_stays_unknown_and_failed_is_not_retryable():
    def handler(request):
        return json_response(request, {"data": {"status": "suspended", "progress": 50}})

    adapter = adapter_with(handler)
    assert adapter.status("video-1")["status"] == "unknown"

    def failed_handler(request):
        return json_response(request, {"data": {"status": "failed", "progress": 100}})

    adapter = adapter_with(failed_handler)
    failed = adapter.status("video-1")
    assert failed["status"] == "failed"
    assert failed["retryable"] is False


def test_artifact_returns_binary_stream_not_publishable_url():
    def handler(request):
        return httpx.Response(
            200,
            content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
            headers={"content-type": "video/mp4", "content-length": "28"},
            request=request,
        )

    adapter = adapter_with(handler)
    adapter._artifact_urls["video-1"] = "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001
    stream = adapter.artifact("video-1")
    assert not isinstance(stream, str)
    assert stream.read() == b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"


def test_artifact_rejects_non_mp4_and_wrong_origin():
    adapter = adapter_with(
        lambda request: httpx.Response(
            200,
            content=b"not-mp4",
            headers={"content-type": "application/octet-stream"},
            request=request,
        )
    )
    adapter._artifact_urls["video-1"] = "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001
    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter.artifact("video-1")
    assert err.value.code == "ARTIFACT_CONTENT_TYPE_INVALID"

    with pytest.raises(TalkingHeadArtifactError):
        adapter._validated_artifact_url("https://evil.example/video.mp4")  # noqa: SLF001
    with pytest.raises(TalkingHeadArtifactError):
        adapter._validated_artifact_url("https://cdn.heygen.com/video.mp4?token=secret")  # noqa: SLF001


def test_artifact_accepts_approved_media_origin():
    # Control-plane and media origins are intentionally decoupled.
    assert HEYGEN_API_ORIGIN != HEYGEN_MEDIA_ORIGIN
    assert HEYGEN_MEDIA_ORIGIN == ("https", "cdn.heygen.com", None)
    assert HEYGEN_ARTIFACT_ORIGINS == (HEYGEN_MEDIA_ORIGIN,)

    adapter = adapter_with(lambda request: httpx.Response(200, request=request))
    approved = "https://cdn.heygen.com/videos/video-1.mp4"
    assert adapter._validated_artifact_url(approved) == approved  # noqa: SLF001

    def handler(request):
        assert str(request.url) == approved
        return httpx.Response(
            200,
            content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
            headers={"content-type": "video/mp4", "content-length": "28"},
            request=request,
        )

    adapter = adapter_with(handler)
    adapter._artifact_urls["video-1"] = approved  # noqa: SLF001
    assert adapter.artifact("video-1").read() == b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"


def test_artifact_rejects_unknown_origin():
    adapter = adapter_with(lambda request: httpx.Response(200, request=request))
    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter._validated_artifact_url("https://evil.example/video.mp4")  # noqa: SLF001
    assert err.value.code == "ARTIFACT_ORIGIN_REJECTED"
    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter._validated_artifact_url("https://api.heygen.com/videos/video-1.mp4")  # noqa: SLF001
    assert err.value.code == "ARTIFACT_ORIGIN_REJECTED"


def test_artifact_rejects_redirect_from_approved_media_origin():
    def handler(request):
        return httpx.Response(
            302, headers={"location": "https://cdn.heygen.com/other.mp4"}, request=request
        )

    adapter = adapter_with(handler)
    adapter._artifact_urls["video-1"] = "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001
    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter.artifact("video-1")
    assert err.value.code == "ARTIFACT_REDIRECT_REJECTED"


def test_artifact_rejects_redirect_and_missing_source():
    adapter = adapter_with(
        lambda request: httpx.Response(
            302, headers={"location": "https://evil.example/a.mp4"}, request=request
        )
    )
    adapter._artifact_urls["video-1"] = "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001
    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter.artifact("video-1")
    assert err.value.code == "ARTIFACT_REDIRECT_REJECTED"

    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter_with(lambda request: httpx.Response(200, request=request)).artifact("missing")
    assert err.value.code == "ARTIFACT_ID_UNKNOWN"


def test_serialized_config_does_not_leak_credentials(caplog):
    adapter = adapter_with(lambda request: json_response(request, {"data": {"video_id": "v1"}}))
    adapter.submit(valid_spec())
    serialized = str({"health": adapter.check_health(), "caps": adapter.get_capabilities()}).lower()
    assert "authorization" not in serialized
    assert "x-api-key" not in serialized
    # The variable name is part of the configuration contract and may appear;
    # no secret *value* is ever present because the adapter never reads it.
    assert "heygen_api_key" in serialized


def test_adapter_version_is_pinned():
    assert ADAPTER_VERSION == "aether-talking-head-v1"
