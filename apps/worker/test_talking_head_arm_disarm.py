"""A1-P3: secret boundary, signed transport, arm/disarm, and fail-closed canary prep.

Every test is fake-only: a ``httpx.MockTransport`` delegate stands in for the
real HeyGen API and no Provider request is ever issued.  These tests prove the
full pre-canary safety surface:

* default disabled and missing/invalid secret both yield zero Provider requests;
* the signed transport injects ``X-Api-Key`` without ever storing or leaking the
  secret value;
* read-only identity preflight is strictly separated from generation authority;
* arm binding mismatch and disarm both restore a zero-request capability;
* 401/402/429/5xx, timeout/ambiguous, unknown artifact host, and redirect all
  fail closed without retry/replay;
* the first canary character input is limited to ``avatar_id``.
"""
from __future__ import annotations

import os
import stat
import tempfile

import httpx
import pytest

from app.talking_head_arm import (
    ARM_BINDING,
    HEYGEN_API_KEY_ENV_VAR,
    HEYGEN_API_KEY_FILE_ENV_VAR,
    SecretRef,
    SignedApiKeyTransport,
    TalkingHeadArmController,
    TalkingHeadArmMode,
)
from app.talking_head_provider_adapter import (
    HeyGenTalkingHeadAdapter,
    TalkingHeadAmbiguousSubmissionError,
    TalkingHeadArtifactError,
    TalkingHeadJobSpec,
    TalkingHeadProviderDisabled,
    TalkingHeadProviderError,
)

FAKE_SECRET = "fake-heygen-key-9f2c3b8a7d6e"


def clear_secret_env(monkeypatch):
    monkeypatch.delenv(HEYGEN_API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(HEYGEN_API_KEY_FILE_ENV_VAR, raising=False)


def valid_spec(**overrides):
    values = dict(
        text="大家好，这是一条十秒口播测试。",
        duration_seconds=10,
        aspect_ratio="9:16",
        resolution="1080p",
        voice_id="voice-zh-CN-1",
        avatar_id="avatar-42",
    )
    values.update(overrides)
    return TalkingHeadJobSpec(**values)


def delegate_with(calls, handler):
    return httpx.MockTransport(handler)


# ------------------------------------------------------------------ disabled
def test_controller_defaults_to_disabled():
    assert TalkingHeadArmController.from_env().mode is TalkingHeadArmMode.DISABLED
    assert TalkingHeadArmController.from_env().is_disabled()


def test_disabled_builds_off_adapter_with_zero_requests(monkeypatch):
    clear_secret_env(monkeypatch)
    adapter = TalkingHeadArmController.from_env().build_adapter(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    )
    assert isinstance(adapter, HeyGenTalkingHeadAdapter)
    assert adapter.enabled is False
    assert adapter.check_health()["status"] == "disabled"
    with pytest.raises(TalkingHeadProviderDisabled) as err:
        adapter.submit(valid_spec())
    assert err.value.code == "PROVIDER_DISABLED"


def test_missing_secret_zero_requests(monkeypatch):
    clear_secret_env(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True}, request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    assert adapter.enabled is True and adapter.identity_only is True
    with pytest.raises(TalkingHeadProviderError) as err:
        adapter.identity_preflight()
    assert err.value.code == "SECRET_MISSING"
    assert calls == []


def test_secret_permission_invalid_zero_requests(monkeypatch, tmp_path):
    clear_secret_env(monkeypatch)
    path = tmp_path / "heygen.key"
    path.write_text(FAKE_SECRET)
    path.chmod(0o644)  # group/other readable -> invalid
    monkeypatch.setenv(HEYGEN_API_KEY_FILE_ENV_VAR, str(path))

    secret = SecretRef()
    assert secret.present() is True
    assert secret.permission_ok() is False
    with pytest.raises(TalkingHeadProviderError) as err:
        secret.resolve()
    assert err.value.code == "SECRET_PERMISSION_INVALID"


def test_secret_ref_never_leaks_value(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    secret = SecretRef()
    assert secret.present() is True
    assert secret.permission_ok() is True
    assert FAKE_SECRET not in repr(secret)
    assert FAKE_SECRET not in str(secret)
    assert "redacted" in repr(secret)
    # resolve() materializes the transient value only for the caller.
    assert secret.resolve() == FAKE_SECRET


# ------------------------------------------------------------ signed transport
def test_signed_transport_injects_x_api_key_without_storing(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    seen_headers = []

    def handler(request):
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = SignedApiKeyTransport(SecretRef(), httpx.MockTransport(handler))
    client = httpx.Client(transport=transport, trust_env=False, follow_redirects=False)
    client.get("https://api.heygen.com/v3/users/me")
    assert seen_headers[0].get("x-api-key") == FAKE_SECRET
    assert FAKE_SECRET not in repr(transport)


def test_signed_transport_does_not_leak_secret_on_error(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "unauthorized"}, request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True,
        identity_only=True,
        transport=SignedApiKeyTransport(SecretRef(), httpx.MockTransport(handler)),
    )
    with pytest.raises(TalkingHeadProviderError) as err:
        adapter.identity_preflight()
    # The secret must never surface in the raised error or evidence.
    assert FAKE_SECRET not in str(err.value)
    assert FAKE_SECRET not in repr(err.value)
    assert FAKE_SECRET not in str(err.value.__dict__)
    assert len(calls) == 1


# ---------------------------------------------------------- preflight isolation
def test_preflight_success_does_not_authorize_generation(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers.get("x-api-key") == FAKE_SECRET
        if request.url.path == "/v3/users/me":
            return httpx.Response(200, json={"data": {"email": "owner@example.test"}}, request=request)
        return httpx.Response(500, request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    result = adapter.identity_preflight()
    assert result["status"] == "ok"
    assert result["provider"] == "heygen"
    assert result["accountIdentified"] is True

    # Generation remains blocked even after a successful read-only preflight.
    with pytest.raises(TalkingHeadProviderDisabled) as err:
        adapter.submit(valid_spec())
    assert err.value.code == "PROVIDER_DISABLED"
    with pytest.raises(TalkingHeadProviderDisabled):
        adapter.status("video-1")
    with pytest.raises(TalkingHeadProviderDisabled):
        adapter.artifact("video-1")
    assert [c.method for c in calls] == ["GET"]


def test_identity_preflight_redirect_rejected(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/"}, request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(TalkingHeadProviderError) as err:
        adapter.identity_preflight()
    assert err.value.code == "PROVIDER_IDENTITY_MISMATCH"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(401, "PROVIDER_UNAUTHORIZED"), (402, "PROVIDER_BALANCE_INSUFFICIENT"),
     (429, "PROVIDER_RATE_LIMITED"), (503, "PROVIDER_5XX")],
)
def test_identity_preflight_http_errors_fail_closed_no_retry(monkeypatch, status, expected_code):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "private"}, request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(TalkingHeadProviderError) as err:
        adapter.identity_preflight()
    assert err.value.code == expected_code
    assert err.value.status_code == status
    assert len(calls) == 1  # never retried


# ------------------------------------------------------------------- arm/disarm
def test_arm_binding_mismatch_fails_closed(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    bad = TalkingHeadArmController(
        mode=TalkingHeadArmMode.ARMED,
        binding=("https", "api.heygen.com", "HEYGEN_API_KEY", "wrong-profile"),
    )
    assert bad.binding_valid() is False
    adapter = bad.build_adapter(transport=httpx.MockTransport(lambda r: httpx.Response(500, request=r)))
    assert adapter.enabled is False  # fail-closed to disabled


def test_armed_adapter_requires_avatar_id_and_unverified_host(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.ARMED)
    adapter = controller.build_adapter(transport=httpx.MockTransport(lambda r: httpx.Response(500, request=r)))
    assert adapter.enabled is True
    assert adapter.identity_only is False
    assert adapter.require_avatar_only is True
    assert adapter.artifact_host_verified is False

    # image_url is not allowed as the first-canary character input.
    with pytest.raises(TalkingHeadProviderError) as err:
        adapter.submit(valid_spec(avatar_id=None, image_url="https://static.example/photo.jpg"))
    assert err.value.code == "AVATAR_ID_REQUIRED"


def test_disarm_restores_zero_request_capability(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)

    armed = TalkingHeadArmController(mode=TalkingHeadArmMode.ARMED)
    adapter = armed.build_adapter(transport=httpx.MockTransport(handler))
    assert adapter.enabled is True

    disarmed = armed.disarm()
    assert disarmed.is_disabled()
    off_adapter = disarmed.build_adapter(transport=httpx.MockTransport(handler))
    assert off_adapter.enabled is False
    with pytest.raises(TalkingHeadProviderDisabled):
        off_adapter.submit(valid_spec())
    with pytest.raises(TalkingHeadProviderDisabled):
        off_adapter.identity_preflight()
    assert calls == []


# ---------------------------------------------------------- artifact host policy
def test_unknown_artifact_host_stops_before_download(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    get_calls = []

    def handler(request):
        if request.url.path == "/v3/videos/video-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "video_id": "video-1",
                        "status": "completed",
                        "video_url": "https://media.unknown-cdn.example/videos/video-1.mp4",
                    }
                },
                request=request,
            )
        if request.url.path.startswith("/videos/"):
            get_calls.append(str(request.url))
        return httpx.Response(200, request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.ARMED)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    status = adapter.status("video-1")
    assert status["status"] == "completed"
    assert status["artifactHost"] == "media.unknown-cdn.example"
    assert status["artifactHostVerified"] is False
    assert adapter.observed_artifact_hosts() == ("media.unknown-cdn.example",)

    with pytest.raises(TalkingHeadArtifactError) as err:
        adapter.artifact("video-1")
    assert err.value.code == "ARTIFACT_HOST_UNVERIFIED"
    assert get_calls == []  # never GET the artifact


def test_artifact_redirect_still_rejected(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    verified = HeyGenTalkingHeadAdapter(
        enabled=True,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://cdn.heygen.com/other.mp4"}, request=request
            )
        ),
        artifact_host_verified=True,
    )
    verified._artifact_urls["video-1"] = "https://cdn.heygen.com/videos/video-1.mp4"  # noqa: SLF001
    with pytest.raises(TalkingHeadArtifactError) as err:
        verified.artifact("video-1")
    assert err.value.code == "ARTIFACT_REDIRECT_REJECTED"


# ---------------------------------------------------------- at-most-once / timeout
def test_submit_timeout_is_ambiguous_and_never_replayed(monkeypatch):
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.ARMED)
    adapter = controller.build_adapter(transport=httpx.MockTransport(handler))
    with pytest.raises(TalkingHeadAmbiguousSubmissionError) as err:
        adapter.submit(valid_spec())
    assert err.value.code == "AMBIGUOUS_SUBMISSION"
    assert len(calls) == 1


def test_secret_value_absent_from_all_evidence(monkeypatch):
    """The secret value must never appear in any sanitized evidence surface."""
    clear_secret_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    secret = SecretRef()
    adapter = HeyGenTalkingHeadAdapter(enabled=True, identity_only=True,
                                       transport=SignedApiKeyTransport(secret, httpx.MockTransport(
                                           lambda r: httpx.Response(500, request=r))))
    evidence = str({
        "secret": repr(secret),
        "health": adapter.check_health(),
        "caps": adapter.get_capabilities(),
    })
    assert FAKE_SECRET not in evidence
