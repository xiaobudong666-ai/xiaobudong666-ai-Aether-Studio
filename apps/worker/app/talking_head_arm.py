"""A1-P3: secret boundary, signed transport, and arm/disarm controller.

This module owns the credential boundary and the operator arm/disarm switch for
the talking-head Provider.  It is preparation-only for the A1 real-provider
canary and performs **zero** Provider network I/O by itself.

Design rules (all fail-closed):

* The secret is referenced **by variable name / file path only** and resolved
  lazily into the signed transport immediately before a request is issued.
  Its value materializes **only** on the outbound request headers at the HTTP
  signing boundary, and is scrubbed immediately after the delegate call; it is
  never persisted, cached, logged, returned, or written into evidence,
  business objects, or exception text.
* Default mode is ``disabled``: no secret resolution, no transport, no request.
* ``preflight`` mode only permits the read-only identity preflight
  (``GET /v3/users/me``); generation (submit/status/artifact) stays blocked.
* ``armed`` mode permits generation only when the frozen binding matches and the
  secret is present with valid permissions.
* Disarm simply returns the switch to ``disabled``: a freshly built adapter then
  has no transport and can never issue a Provider request.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

import httpx

from .talking_head_provider_adapter import (
    HEYGEN_API_KEY_ENV_VAR,
    HEYGEN_AVATAR_VIDEO_PATH,
    HeyGenTalkingHeadAdapter,
    TalkingHeadAmbiguousSubmissionError,
    TalkingHeadProviderError,
)

TALKING_HEAD_MODE_ENV_VAR = "AETHER_TALKING_HEAD_MODE"
HEYGEN_API_KEY_FILE_ENV_VAR = "HEYGEN_API_KEY_FILE"

# Frozen canary binding: the only combination this code will ever arm.  It
# contains no secret material, only stable identifiers that must all match.
ARM_BINDING = (
    "https",
    "api.heygen.com",
    HEYGEN_API_KEY_ENV_VAR,
    "talking-head-canary-v1",
)

# Controlled HeyGen egress: the only permitted route to the Provider is either a
# direct connection (default) or the explicitly configured, strictly pinned
# loopback HTTP proxy below.  No system proxy environment is ever consulted and
# no other host/port is accepted; an invalid value fails closed.
HEYGEN_EGRESS_PROXY_ENV_VAR = "AETHER_TALKING_HEAD_EGRESS_PROXY"
HEYGEN_EGRESS_PROXY_HOST = "127.0.0.1"
HEYGEN_EGRESS_PROXY_PORT = 7890
HEYGEN_EGRESS_PROXY_URL = f"http://{HEYGEN_EGRESS_PROXY_HOST}:{HEYGEN_EGRESS_PROXY_PORT}"


def parse_heygen_egress_proxy(value: str) -> str:
    """Validate a loopback egress proxy URL and return its canonical form.

    Fail closed unless ``value`` is exactly the pinned loopback HTTP proxy
    ``http://127.0.0.1:7890``: scheme ``http`` only, hostname ``127.0.0.1``
    only, port ``7890`` only, and no credentials, query, fragment, or non-root
    path.  Wildcard hosts, non-loopback hosts, and arbitrary ports are refused.
    """
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise TalkingHeadProviderError(
            "HeyGen egress proxy URL is invalid", code="PROXY_POLICY_VIOLATION"
        ) from exc
    if (
        scheme != "http"
        or hostname != HEYGEN_EGRESS_PROXY_HOST
        or port != HEYGEN_EGRESS_PROXY_PORT
        or username is not None
        or password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise TalkingHeadProviderError(
            "HeyGen egress proxy violates the loopback policy",
            code="PROXY_POLICY_VIOLATION",
        )
    return HEYGEN_EGRESS_PROXY_URL


def build_heygen_transport() -> httpx.BaseTransport:
    """Build the HeyGen outbound transport with zero retries and no fallback.

    * Default (``AETHER_TALKING_HEAD_EGRESS_PROXY`` unset): a direct
      ``trust_env=False`` transport.  System ``HTTP_PROXY``/``HTTPS_PROXY`` are
      never consulted.
    * Explicitly set: route exclusively through the pinned loopback proxy.  An
      invalid value fails closed (``PROXY_POLICY_VIOLATION``) and never falls
      back to a direct connection or to a system proxy.
    """
    raw = os.environ.get(HEYGEN_EGRESS_PROXY_ENV_VAR, "").strip()
    if not raw:
        return httpx.HTTPTransport(trust_env=False, retries=0)
    proxy_url = parse_heygen_egress_proxy(raw)
    return httpx.HTTPTransport(proxy=proxy_url, trust_env=False, retries=0)


class TalkingHeadArmMode(str, Enum):
    DISABLED = "disabled"
    PREFLIGHT = "preflight"
    ARMED = "armed"

    @classmethod
    def parse(cls, value: str | None) -> "TalkingHeadArmMode":
        if value in {cls.DISABLED.value, cls.PREFLIGHT.value, cls.ARMED.value}:
            return cls(value)
        return cls.DISABLED  # unknown values fail closed to disabled


@dataclass(frozen=True)
class SecretRef:
    """Credential reference resolved by variable name / file path only.

    ``resolve()`` returns the transient secret value.  Callers must only use it
    at the outbound HTTP signing boundary (see ``SignedApiKeyTransport``); it
    must never be persisted, cached, logged, returned, or written into
    evidence/business objects.  ``repr``/``str`` never leak it.
    """

    env_var: str = HEYGEN_API_KEY_ENV_VAR
    file_env_var: str = HEYGEN_API_KEY_FILE_ENV_VAR

    def __repr__(self) -> str:
        return (
            f"SecretRef(env_var={self.env_var!r}, "
            f"file_env_var={self.file_env_var!r}, redacted=True)"
        )

    __str__ = __repr__

    def source(self) -> str:
        """Return the repo-external source name, never its content."""
        if os.environ.get(self.file_env_var):
            return "file"
        if os.environ.get(self.env_var):
            return "env"
        return "absent"

    def present(self) -> bool:
        return self.source() != "absent"

    def permission_ok(self) -> bool:
        path = os.environ.get(self.file_env_var)
        if path:
            try:
                info = os.stat(path)
            except OSError:
                return False
            if not stat.S_ISREG(info.st_mode):
                return False
            # Owner-only read/write; reject any group/other access.
            return (info.st_mode & 0o077) == 0
        return True  # env-var source has no filesystem permission surface

    def resolve(self) -> str:
        if self.source() == "absent":
            raise TalkingHeadProviderError(
                "Talking-head secret is not configured",
                code="SECRET_MISSING",
            )
        if not self.permission_ok():
            raise TalkingHeadProviderError(
                "Talking-head secret source has invalid permissions",
                code="SECRET_PERMISSION_INVALID",
            )
        path = os.environ.get(self.file_env_var)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError as exc:
                raise TalkingHeadProviderError(
                    "Talking-head secret file could not be read",
                    code="SECRET_MISSING",
                ) from exc
        else:
            value = os.environ.get(self.env_var, "")
        if not value:
            raise TalkingHeadProviderError(
                "Talking-head secret is empty",
                code="SECRET_MISSING",
            )
        return value


class SignedApiKeyTransport(httpx.BaseTransport):
    """Injects ``X-Api-Key`` from a ``SecretRef`` for each outbound request.

    The secret materializes only on the outbound request headers for the
    duration of the delegate call and is scrubbed from the original request in
    a ``finally`` block regardless of success or failure.  Delegate exceptions
    are converted to a fixed, sanitized ``TalkingHeadProviderError`` so the
    secret never escapes through message, repr, ``__dict__``, or the chained
    exception surface (``__cause__`` / ``__context__``).
    """

    def __init__(self, secret: SecretRef, delegate: httpx.BaseTransport):
        self._secret = secret
        self._delegate = delegate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["X-Api-Key"] = self._secret.resolve()
        response: httpx.Response | None = None
        error: TalkingHeadProviderError | None = None
        try:
            response = self._delegate.handle_request(request)
        except TalkingHeadProviderError as exc:
            # Already a sanitized boundary error; re-raise it unchanged.
            error = exc
        except httpx.TimeoutException:
            error = self._read_boundary_error(request)
        except httpx.ReadError:
            error = self._read_boundary_error(request)
        except httpx.ConnectError:
            error = TalkingHeadProviderError(
                "Signed HeyGen connection failed", code="PROVIDER_CONNECTION_FAILED"
            )
        except httpx.ProxyError:
            # A configured loopback egress proxy that is unreachable must fail
            # closed to the same connection error; never fall back to direct.
            error = TalkingHeadProviderError(
                "Signed HeyGen proxy connection failed", code="PROVIDER_CONNECTION_FAILED"
            )
        except Exception:
            error = TalkingHeadProviderError(
                "Signed HeyGen request failed", code="SIGNED_REQUEST_FAILED"
            )
        finally:
            # Scrub the credential from the caller's request object whether the
            # delegate returned or raised, so no request/header reference kept
            # by the response or by an exception can expose the key.
            request.headers.pop("X-Api-Key", None)
        if error is not None:
            # Raised outside the ``except`` block so the delegate exception is
            # never implicitly chained onto this sanitized error.
            raise error
        assert response is not None  # pragma: no cover - delegate returned
        return response

    @staticmethod
    def _read_boundary_error(request: httpx.Request) -> TalkingHeadProviderError:
        """Map a delegate read/timeout failure to the correct fail-closed error.

        A read failure on the single-submission ``POST /v3/videos`` path is an
        ambiguous outcome: the Provider may already have accepted the job, so
        the adapter must treat it as UNKNOWN and never auto-replay it.  Every
        other path (identity preflight, status polling, artifact fetch) maps to
        a generic read timeout that is reported but never retried automatically.
        """
        if (
            request.method.upper() == "POST"
            and request.url.path == HEYGEN_AVATAR_VIDEO_PATH
        ):
            return TalkingHeadAmbiguousSubmissionError()
        return TalkingHeadProviderError(
            "Signed HeyGen request timed out", code="PROVIDER_READ_TIMEOUT"
        )

    def close(self) -> None:
        self._delegate.close()


@dataclass(frozen=True)
class TalkingHeadArmController:
    """Operator arm/disarm switch and frozen canary binding validation."""

    mode: TalkingHeadArmMode = TalkingHeadArmMode.DISABLED
    binding: tuple[str, ...] = ARM_BINDING

    @classmethod
    def from_env(cls) -> "TalkingHeadArmController":
        return cls(mode=TalkingHeadArmMode.parse(
            os.environ.get(TALKING_HEAD_MODE_ENV_VAR)
        ))

    def is_disabled(self) -> bool:
        return self.mode is TalkingHeadArmMode.DISABLED

    def can_preflight(self) -> bool:
        return self.mode in {
            TalkingHeadArmMode.PREFLIGHT,
            TalkingHeadArmMode.ARMED,
        }

    def can_generate(self) -> bool:
        return self.mode is TalkingHeadArmMode.ARMED

    def binding_valid(self) -> bool:
        return self.binding == ARM_BINDING

    def armed_and_bound(self) -> bool:
        return self.can_generate() and self.binding_valid()

    def disarm(self) -> "TalkingHeadArmController":
        """Return a disabled controller; a rebuilt adapter then issues 0 calls."""
        return TalkingHeadArmController(
            mode=TalkingHeadArmMode.DISABLED, binding=self.binding
        )

    def readiness(self, secret: SecretRef | None = None) -> dict:
        """Sanitized readiness report; never contains the secret value."""
        secret = secret or SecretRef()
        secret_present = secret.present()
        secret_permission_ok = secret.permission_ok()
        generation_authorized = (
            self.armed_and_bound() and secret_present and secret_permission_ok
        )
        return {
            "mode": self.mode.value,
            "bindingValid": self.binding_valid(),
            "secretPresent": secret_present,
            "secretPermissionOk": secret_permission_ok,
            "generationAuthorized": generation_authorized,
            "preflightAuthorized": (
                self.can_preflight() and secret_present and secret_permission_ok
            ),
        }

    def build_adapter(
        self,
        transport: httpx.BaseTransport | None = None,
        secret: SecretRef | None = None,
    ) -> HeyGenTalkingHeadAdapter:
        """Build the adapter that matches the current arm state.

        ``disabled`` returns a default-off adapter with no transport (0 request
        capability).  ``preflight`` returns an identity-only adapter.  ``armed``
        returns a generation-capable adapter only when the binding matches;
        otherwise it fails closed to disabled.
        """
        if self.is_disabled():
            return HeyGenTalkingHeadAdapter()
        if transport is None:
            transport = build_heygen_transport()
        secret = secret or SecretRef()
        signed = SignedApiKeyTransport(secret, transport)
        if self.mode is TalkingHeadArmMode.PREFLIGHT:
            return HeyGenTalkingHeadAdapter(
                enabled=True,
                identity_only=True,
                transport=signed,
            )
        if not self.binding_valid():
            return HeyGenTalkingHeadAdapter()
        return HeyGenTalkingHeadAdapter(
            enabled=True,
            identity_only=False,
            transport=signed,
            require_avatar_only=True,
            artifact_host_verified=False,  # first canary: stop before download until host frozen
        )
