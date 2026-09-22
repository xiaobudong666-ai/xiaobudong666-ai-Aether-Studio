"""A1-P4: controlled HeyGen egress proxy transport (fake-only).

Every test uses a fake ``httpx.MockTransport`` delegate or introspects the
transport configuration; no real Provider request, no real proxy connection, and
no real secret are ever used.  These tests prove the loopback egress policy:

* the proxy is default-off and only ``http://127.0.0.1:7890`` can be selected;
* system ``HTTP_PROXY``/``HTTPS_PROXY`` never change routing (``trust_env=False``);
* non-loopback hosts, wrong ports, and malformed URLs are rejected fail-closed;
* an unreachable proxy fails closed to a connection error and never falls back
  to a direct connection;
* the Provider destination stays pinned to ``api.heygen.com:443``;
* secret scrubbing, preflight/generation separation, and disarm remain intact.
"""
from __future__ import annotations

import httpx
import pytest

from app.talking_head_arm import (
    HEYGEN_API_KEY_ENV_VAR,
    HEYGEN_API_KEY_FILE_ENV_VAR,
    HEYGEN_EGRESS_PROXY_ENV_VAR,
    HEYGEN_EGRESS_PROXY_URL,
    SecretRef,
    SignedApiKeyTransport,
    TalkingHeadArmController,
    TalkingHeadArmMode,
    build_heygen_transport,
    parse_heygen_egress_proxy,
)
from app.talking_head_provider_adapter import (
    HeyGenTalkingHeadAdapter,
    TalkingHeadIdentityError,
    TalkingHeadJobSpec,
    TalkingHeadProviderDisabled,
    TalkingHeadProviderError,
)

FAKE_SECRET = "fake-heygen-key-9f2c3b8a7d6e"


def clear_env(monkeypatch):
    for name in (
        HEYGEN_API_KEY_ENV_VAR,
        HEYGEN_API_KEY_FILE_ENV_VAR,
        HEYGEN_EGRESS_PROXY_ENV_VAR,
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def pool_kind(transport: httpx.BaseTransport) -> str:
    return type(transport._pool).__name__


def valid_spec(**overrides) -> TalkingHeadJobSpec:
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


# ------------------------------------------------------------- proxy policy
def test_proxy_defaults_to_direct_transport(monkeypatch):
    clear_env(monkeypatch)
    transport = build_heygen_transport()
    assert pool_kind(transport) == "ConnectionPool"


def test_explicit_loopback_builds_proxy_transport(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, "http://127.0.0.1:7890")
    transport = build_heygen_transport()
    assert pool_kind(transport) == "HTTPProxy"
    assert transport._pool._proxy_url.host == b"127.0.0.1"
    assert transport._pool._proxy_url.port == 7890


def test_trailing_slash_is_canonicalized(monkeypatch):
    clear_env(monkeypatch)
    assert parse_heygen_egress_proxy("http://127.0.0.1:7890/") == HEYGEN_EGRESS_PROXY_URL


@pytest.mark.parametrize(
    "bad",
    [
        "http://10.0.0.1:7890",
        "http://127.0.0.2:7890",
        "http://localhost:7890",
        "http://[::1]:7890",
        "http://example.com:7890",
        "http://127.0.0.1.nip.io:7890",
    ],
)
def test_non_loopback_proxy_rejected(monkeypatch, bad):
    clear_env(monkeypatch)
    with pytest.raises(TalkingHeadProviderError) as err:
        parse_heygen_egress_proxy(bad)
    assert err.value.code == "PROXY_POLICY_VIOLATION"


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:8080",
        "http://127.0.0.1:0",
        "http://127.0.0.1",
        "http://127.0.0.1:abc",
    ],
)
def test_wrong_or_missing_port_rejected(monkeypatch, bad):
    clear_env(monkeypatch)
    with pytest.raises(TalkingHeadProviderError) as err:
        parse_heygen_egress_proxy(bad)
    assert err.value.code == "PROXY_POLICY_VIOLATION"


@pytest.mark.parametrize(
    "bad",
    [
        "https://127.0.0.1:7890",
        "socks5://127.0.0.1:7890",
        "http://user:pass@127.0.0.1:7890",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:7890?x=1",
        "http://127.0.0.1:7890#frag",
    ],
)
def test_invalid_proxy_urls_rejected(monkeypatch, bad):
    clear_env(monkeypatch)
    with pytest.raises(TalkingHeadProviderError) as err:
        parse_heygen_egress_proxy(bad)
    assert err.value.code == "PROXY_POLICY_VIOLATION"


def test_system_proxy_env_cannot_change_routing(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://evil.example:8080")
    transport = build_heygen_transport()
    # trust_env=False means the system proxy variables are ignored: direct pool.
    assert pool_kind(transport) == "ConnectionPool"


def test_system_proxy_env_cannot_override_explicit_loopback(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:8080")
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, HEYGEN_EGRESS_PROXY_URL)
    transport = build_heygen_transport()
    assert pool_kind(transport) == "HTTPProxy"
    assert transport._pool._proxy_url.host == b"127.0.0.1"
    assert transport._pool._proxy_url.port == 7890


def test_invalid_env_value_build_fails_closed(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, "http://127.0.0.1:8080")
    with pytest.raises(TalkingHeadProviderError) as err:
        build_heygen_transport()
    assert err.value.code == "PROXY_POLICY_VIOLATION"


# ----------------------------------------------------- fail-closed / no fallback
def test_proxy_failure_does_not_fall_back_to_direct(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ProxyError("proxy unreachable")

    signed = SignedApiKeyTransport(SecretRef(), httpx.MockTransport(handler))
    client = httpx.Client(transport=signed, trust_env=False, follow_redirects=False)
    with pytest.raises(TalkingHeadProviderError) as err:
        client.get("https://api.heygen.com/v3/users/me")
    assert err.value.code == "PROVIDER_CONNECTION_FAILED"
    assert len(calls) == 1  # no retry, no fallback to a second connection


# ------------------------------------------------------------ destination pin
def test_destination_must_stay_pinned_to_heygen(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, HEYGEN_EGRESS_PROXY_URL)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter()
    with pytest.raises(TalkingHeadIdentityError):
        adapter._assert_identity("https://api.evil.com/v3/users/me")
    # The legitimate pinned origin still validates.
    adapter._assert_identity("https://api.heygen.com/v3/users/me")


# ------------------------------------------------- secret / preflight / disarm
def test_proxy_path_still_never_leaks_secret(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, HEYGEN_EGRESS_PROXY_URL)

    def handler(request):
        raise RuntimeError(f"boom with {FAKE_SECRET} and {dict(request.headers)}")

    signed = SignedApiKeyTransport(SecretRef(), httpx.MockTransport(handler))
    client = httpx.Client(transport=signed, trust_env=False, follow_redirects=False)
    with pytest.raises(TalkingHeadProviderError) as err:
        client.get("https://api.heygen.com/v3/users/me")
    assert FAKE_SECRET not in str(err.value)
    assert FAKE_SECRET not in repr(err.value)
    assert FAKE_SECRET not in str(err.value.__dict__)


def test_preflight_still_cannot_generate_with_proxy(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, HEYGEN_EGRESS_PROXY_URL)

    controller = TalkingHeadArmController(mode=TalkingHeadArmMode.PREFLIGHT)
    adapter = controller.build_adapter()
    assert adapter.enabled is True
    assert adapter.identity_only is True
    with pytest.raises(TalkingHeadProviderDisabled):
        adapter.submit(valid_spec())


def test_disarm_restores_zero_request_capability_with_proxy(monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv(HEYGEN_API_KEY_ENV_VAR, FAKE_SECRET)
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, HEYGEN_EGRESS_PROXY_URL)

    armed = TalkingHeadArmController(mode=TalkingHeadArmMode.ARMED)
    disarmed = armed.disarm()
    assert disarmed.is_disabled()
    adapter = disarmed.build_adapter()
    assert adapter.check_health()["status"] == "disabled"
    assert adapter.check_health()["responsive"] is False
    with pytest.raises(TalkingHeadProviderDisabled):
        adapter.submit(valid_spec())


def test_disabled_never_validates_proxy_policy(monkeypatch):
    clear_env(monkeypatch)
    # Even a deliberately invalid proxy value must not matter while disabled.
    monkeypatch.setenv(HEYGEN_EGRESS_PROXY_ENV_VAR, "http://127.0.0.1:8080")
    adapter = TalkingHeadArmController.from_env().build_adapter()
    assert adapter.enabled is False
    assert adapter.check_health()["status"] == "disabled"
