"""A1-P3: secret boundary, signed transport, and arm/disarm controller.

This module owns the credential boundary and the operator arm/disarm switch for
the talking-head Provider.  It is preparation-only for the A1 real-provider
canary and performs **zero** Provider network I/O by itself.

Design rules (all fail-closed):

* The secret is referenced **by variable name / file path only** and resolved
  lazily into the signed transport immediately before a request is issued.  Its
  value is never stored on a Python object, never repr'd/logged, and never
  appears in exception text or evidence.
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

import httpx

from .talking_head_provider_adapter import (
    HEYGEN_API_KEY_ENV_VAR,
    HeyGenTalkingHeadAdapter,
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

    ``resolve()`` returns the transient secret value and is the only place the
    value is ever materialized.  ``repr``/``str`` never leak it.
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

    The value lives only on the request headers for the duration of the
    underlying transport call and is never retained or exposed by this object.
    """

    def __init__(self, secret: SecretRef, delegate: httpx.BaseTransport):
        self._secret = secret
        self._delegate = delegate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["X-Api-Key"] = self._secret.resolve()
        return self._delegate.handle_request(request)

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
        secret = secret or SecretRef()
        signed = SignedApiKeyTransport(secret, transport) if transport is not None else None
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
