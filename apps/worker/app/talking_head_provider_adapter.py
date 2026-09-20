"""Vendor-agnostic talking-head Provider adapter (P0 preparation).

This module defines the ``TalkingHeadProviderAdapter`` contract and a first
implementation for HeyGen Photo Avatar / Avatar Video.  It is **default-off**
and is a preparation-only artifact for the A1 real-provider canary:

* 1 task / 1 submission / 1 attempt / 0 retry / 0 fallback
* <= 10 seconds, 9:16, single output
* explicit timeout, ambiguous submission is never replayed
* Provider identity is pinned and any mismatch fails closed
* cost ceiling is enforced before submission and fails closed
* the downloaded artifact is only a byte stream that must re-enter Aether
  artifact intake / Rights / Timeline / Render; it never becomes a published
  asset by itself

The secret is referenced **by variable name only** (``HEYGEN_API_KEY``) in the
configuration contract.  This module never reads, stores, or prints its value.
Tests must be fake-only and never issue a real HeyGen request.
"""
from __future__ import annotations

import io
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.parse import unquote, urlsplit

import httpx

ADAPTER_VERSION = "aether-talking-head-v1"

# Variable name only.  Never read this environment variable in this module;
# authentication for a future real run is attached by the caller-owned,
# already-signed HTTP transport, which keeps the credential boundary out of the
# adapter and therefore out of audit/logging/artifact code.
HEYGEN_API_KEY_ENV_VAR = "HEYGEN_API_KEY"

HEYGEN_API_ORIGIN = ("https", "api.heygen.com", None)
HEYGEN_AVATAR_VIDEO_PATH = "/v3/videos"

# Artifact/media origin is intentionally decoupled from the API control-plane
# origin.  HeyGen's completed-job ``video_url`` points at its media CDN, so the
# downloadable artifact origin must be independently pinned and fail-closed.
HEYGEN_MEDIA_ORIGIN = ("https", "cdn.heygen.com", None)
HEYGEN_ARTIFACT_ORIGINS = (HEYGEN_MEDIA_ORIGIN,)

# Official heygen-stack API reference (Avatar Video direct-control path).
HEYGEN_COST_PER_SECOND_USD = 0.10
HEYGEN_DEFAULT_TIMEOUT_SECONDS = 30.0
HEYGEN_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024  # 512 MiB defensive cap


class TalkingHeadProviderError(Exception):
    """Sanitized, stable error boundary for talking-head adapters."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class TalkingHeadProviderDisabled(TalkingHeadProviderError):
    """Raised when the Provider is default-off / not explicitly enabled."""

    def __init__(self, message: str = "Talking-head Provider is disabled"):
        super().__init__(message, code="PROVIDER_DISABLED", retryable=False)


class TalkingHeadAmbiguousSubmissionError(TalkingHeadProviderError):
    """Raised when the submission outcome is unknown and must never be replayed."""

    def __init__(self, message: str = "Talking-head submission outcome is unknown"):
        super().__init__(message, code="AMBIGUOUS_SUBMISSION", retryable=False)


class TalkingHeadCostCeilingError(TalkingHeadProviderError):
    """Raised when the estimated cost exceeds the canary ceiling."""

    def __init__(self, message: str = "Talking-head cost ceiling exceeded"):
        super().__init__(message, code="COST_CEILING_EXCEEDED", retryable=False)


class TalkingHeadIdentityError(TalkingHeadProviderError):
    """Raised when the upstream identity/origin does not match the pin."""

    def __init__(self, message: str = "Talking-head Provider identity mismatch"):
        super().__init__(message, code="PROVIDER_IDENTITY_MISMATCH", retryable=False)


class TalkingHeadArtifactError(TalkingHeadProviderError):
    """Raised when a Provider artifact fails intake-boundary validation."""


@dataclass(frozen=True)
class TalkingHeadJobSpec:
    """Supplier-agnostic single-output talking-head job contract.

    Exactly one video of ``aspect_ratio`` x ``resolution`` is produced from
    either a photo avatar/image plus TTS script (``text`` + ``voice_id``) or a
    pre-recorded audio source (``audio_url``).
    """

    text: str
    duration_seconds: int
    aspect_ratio: str = "9:16"
    resolution: str = "1080p"
    voice_id: str | None = None
    image_url: str | None = None
    image_asset_id: str | None = None
    avatar_id: str | None = None
    audio_url: str | None = None
    audio_asset_id: str | None = None
    expressiveness: str | None = None
    remove_background: bool = False
    background: dict | None = None
    voice_settings: dict | None = None
    motion_prompt: str | None = None

    def validate(self) -> None:
        if self.aspect_ratio != "9:16":
            raise TalkingHeadProviderError(
                "Talking-head job aspect ratio must be 9:16",
                code="JOB_SPEC_INVALID",
            )
        if self.resolution not in {"1080p", "720p"}:
            raise TalkingHeadProviderError(
                "Talking-head job resolution is unsupported",
                code="JOB_SPEC_INVALID",
            )
        if not 1 <= int(self.duration_seconds) <= 10:
            raise TalkingHeadProviderError(
                "Talking-head job duration must be between 1 and 10 seconds",
                code="JOB_SPEC_INVALID",
            )
        if not self.text.strip() or len(self.text) > 500:
            raise TalkingHeadProviderError(
                "Talking-head job script is invalid",
                code="JOB_SPEC_INVALID",
            )

        has_visual = bool(self.avatar_id or self.image_url or self.image_asset_id)
        if not has_visual:
            raise TalkingHeadProviderError(
                "Talking-head job requires a photo avatar or image source",
                code="JOB_SPEC_INVALID",
            )
        if sum(bool(v) for v in (self.avatar_id, self.image_url, self.image_asset_id)) != 1:
            raise TalkingHeadProviderError(
                "Talking-head job visual source is ambiguous",
                code="JOB_SPEC_INVALID",
            )

        has_audio = bool(self.audio_url or self.audio_asset_id)
        if has_audio:
            if sum(bool(v) for v in (self.audio_url, self.audio_asset_id)) != 1:
                raise TalkingHeadProviderError(
                    "Talking-head job audio source is ambiguous",
                    code="JOB_SPEC_INVALID",
                )
            if self.voice_id:
                raise TalkingHeadProviderError(
                    "Talking-head job cannot combine TTS and pre-recorded audio",
                    code="JOB_SPEC_INVALID",
                )
        elif not self.voice_id:
            raise TalkingHeadProviderError(
                "Talking-head TTS job requires a voice_id",
                code="JOB_SPEC_INVALID",
            )


class TalkingHeadProviderAdapter(ABC):
    """Vendor-neutral talking-head lifecycle contract.

    ``submit`` must be a single submission attempt: it either returns a
    ``job_id`` or raises.  ``status`` is a single poll.  ``artifact`` returns a
    binary stream that the caller must route back through Aether artifact
    intake / Rights / Timeline / Render.
    """

    @abstractmethod
    def submit(self, job_spec: TalkingHeadJobSpec) -> str:
        """Submit exactly one job; returns ``job_id``.  Never auto-replay."""

    @abstractmethod
    def status(self, job_id: str) -> dict:
        """Return a sanitized status dict for a previously submitted job."""

    @abstractmethod
    def artifact(self, job_id: str) -> BinaryIO:
        """Return a validated binary artifact stream (never a publishable URL)."""


class HeyGenTalkingHeadAdapter(TalkingHeadProviderAdapter):
    """HeyGen Avatar Video (photo avatar) adapter.  Default-off, fail-closed."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        transport: httpx.BaseTransport | None = None,
        api_url: str = "https://api.heygen.com",
        timeout: float | None = None,
        cost_ceiling_usd: float = 1.00,
        cost_per_second_usd: float = HEYGEN_COST_PER_SECOND_USD,
        max_artifact_bytes: int | None = None,
        artifact_origins: tuple[tuple[str, str, int | None], ...] | None = None,
    ):
        self.enabled = enabled
        self.timeout = float(
            timeout if timeout is not None else HEYGEN_DEFAULT_TIMEOUT_SECONDS
        )
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.cost_ceiling_usd = float(cost_ceiling_usd)
        self.cost_per_second_usd = float(cost_per_second_usd)
        self.max_artifact_bytes = int(
            max_artifact_bytes if max_artifact_bytes is not None else HEYGEN_MAX_ARTIFACT_BYTES
        )

        raw_artifact_origins = (
            tuple(artifact_origins)
            if artifact_origins is not None
            else HEYGEN_ARTIFACT_ORIGINS
        )
        if not raw_artifact_origins:
            raise ValueError("artifact origins must not be empty")
        normalized_artifact_origins: list[tuple[str, str, int | None]] = []
        for artifact_origin in raw_artifact_origins:
            try:
                scheme, hostname, port = artifact_origin
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "artifact origin must be a (scheme, host, port) tuple"
                ) from exc
            if scheme not in {"http", "https"} or not hostname:
                raise ValueError("artifact origin is invalid")
            normalized_artifact_origins.append((scheme, hostname.lower(), port))
        self._artifact_origins = tuple(normalized_artifact_origins)

        parsed = urlsplit(api_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("HeyGen API origin is invalid")
        self.api_url = api_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname, parsed.port)
        if enabled and self._origin != HEYGEN_API_ORIGIN:
            raise TalkingHeadIdentityError("HeyGen Provider origin does not match the pin")
        self._transport = transport
        self._artifact_urls: dict[str, str] = {}

    # ----------------------------------------------------------------- config
    def check_health(self) -> dict:
        """Local configuration check only; performs no Provider network I/O."""
        return {
            "status": "disabled" if not self.enabled else "configured",
            "service": "heygen-talking-head",
            "responsive": self.enabled and self._transport is not None,
            "adapterVersion": ADAPTER_VERSION,
            "credentialEnvVar": HEYGEN_API_KEY_ENV_VAR,
            "network": "none",
        }

    def get_capabilities(self) -> dict:
        health = self.check_health()
        active = health["status"] == "configured"
        return {
            "status": "active" if active else "disabled",
            "healthy": active,
            "adapterVersion": ADAPTER_VERSION,
            "capabilities": {
                "videoAspects": ["9:16"],
                "resolutions": ["1080p", "720p"],
                "maxOutputs": 1,
                "maxClipDurationSeconds": 10,
                "cancellationSupported": False,
                "artifactStreaming": True,
                "network": "https" if active else "none",
            },
        }

    # ----------------------------------------------------------------- client
    def _client(self) -> httpx.Client:
        if not self.enabled:
            raise TalkingHeadProviderDisabled()
        if self._transport is None:
            raise TalkingHeadProviderDisabled(
                "HeyGen transport is not configured"
            )
        return httpx.Client(
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
        )

    def _assert_identity(self, url: str) -> None:
        parsed = urlsplit(url)
        origin = (parsed.scheme, parsed.hostname, parsed.port)
        if origin != self._origin or origin != HEYGEN_API_ORIGIN:
            raise TalkingHeadIdentityError("HeyGen Provider identity mismatch")
        if parsed.username is not None or parsed.password is not None:
            raise TalkingHeadIdentityError("HeyGen Provider identity mismatch")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except Exception as exc:
            raise TalkingHeadProviderError(
                "HeyGen returned invalid JSON", code="PROVIDER_JSON_INVALID"
            ) from exc
        if not isinstance(payload, dict):
            raise TalkingHeadProviderError(
                "HeyGen returned an invalid object", code="PROVIDER_JSON_INVALID"
            )
        return payload

    def _submit_payload(self, spec: TalkingHeadJobSpec) -> dict:
        payload: dict = {
            "aspect_ratio": spec.aspect_ratio,
            "resolution": spec.resolution,
        }
        if spec.avatar_id:
            payload["avatar_id"] = spec.avatar_id
        elif spec.image_url:
            payload["image_url"] = spec.image_url
        elif spec.image_asset_id:
            payload["image_asset_id"] = spec.image_asset_id

        if spec.audio_url:
            payload["audio_url"] = spec.audio_url
        elif spec.audio_asset_id:
            payload["audio_asset_id"] = spec.audio_asset_id
        else:
            payload["script"] = spec.text
            payload["voice_id"] = spec.voice_id

        if spec.expressiveness is not None:
            payload["expressiveness"] = spec.expressiveness
        if spec.motion_prompt is not None:
            payload["motion_prompt"] = spec.motion_prompt
        if spec.remove_background:
            payload["remove_background"] = spec.remove_background
        if spec.background is not None:
            payload["background"] = spec.background
        if spec.voice_settings is not None:
            payload["voice_settings"] = spec.voice_settings
        return payload

    # ---------------------------------------------------------------- submit
    def submit(self, job_spec: TalkingHeadJobSpec) -> str:
        if not isinstance(job_spec, TalkingHeadJobSpec):
            raise TypeError("job_spec must be a TalkingHeadJobSpec")
        job_spec.validate()
        if not self.enabled:
            raise TalkingHeadProviderDisabled()

        estimated_cost = int(job_spec.duration_seconds) * self.cost_per_second_usd
        if estimated_cost > self.cost_ceiling_usd:
            raise TalkingHeadCostCeilingError(
                f"Estimated cost ${estimated_cost:.2f} exceeds ceiling ${self.cost_ceiling_usd:.2f}"
            )

        payload = self._submit_payload(job_spec)
        url = f"{self.api_url}{HEYGEN_AVATAR_VIDEO_PATH}"
        self._assert_identity(url)
        try:
            with self._client() as client:
                response = client.post(url, json=payload)
        except (httpx.ReadTimeout, httpx.ReadError) as exc:
            raise TalkingHeadAmbiguousSubmissionError() from exc
        except httpx.ConnectError as exc:
            raise TalkingHeadProviderError(
                "HeyGen connection failed", code="PROVIDER_CONNECTION_FAILED"
            ) from exc
        except TalkingHeadProviderError:
            raise
        except Exception as exc:
            raise TalkingHeadProviderError(
                "HeyGen returned an invalid response", code="PROVIDER_RESPONSE_INVALID"
            ) from exc

        if response.status_code in {301, 302, 303, 307, 308}:
            raise TalkingHeadIdentityError("HeyGen redirect rejected")
        if response.status_code >= 400:
            code = (
                "PROVIDER_RATE_LIMITED" if response.status_code == 429
                else "PROVIDER_BALANCE_INSUFFICIENT" if response.status_code == 402
                else "PROVIDER_UNAUTHORIZED" if response.status_code == 401
                else "PROVIDER_5XX" if response.status_code >= 500
                else "PROVIDER_4XX"
            )
            raise TalkingHeadProviderError(
                "HeyGen rejected the request",
                code=code,
                status_code=response.status_code,
            )

        data = self._json_object(response).get("data")
        if not isinstance(data, dict):
            data = self._json_object(response)
        video_id = data.get("video_id") or data.get("videoId")
        if not video_id:
            raise TalkingHeadProviderError(
                "HeyGen omitted video identifier", code="PROVIDER_ID_MISSING"
            )
        return str(video_id)

    # ---------------------------------------------------------------- status
    def status(self, job_id: str) -> dict:
        if not self.enabled:
            raise TalkingHeadProviderDisabled()
        self._validate_job_id(job_id)
        url = f"{self.api_url}{HEYGEN_AVATAR_VIDEO_PATH}/{job_id}"
        self._assert_identity(url)
        try:
            with self._client() as client:
                response = client.get(url)
        except (httpx.ReadTimeout, httpx.ReadError) as exc:
            raise TalkingHeadProviderError(
                "HeyGen status timed out", code="PROVIDER_READ_TIMEOUT", retryable=True
            ) from exc
        except httpx.ConnectError as exc:
            raise TalkingHeadProviderError(
                "HeyGen connection failed", code="PROVIDER_CONNECTION_FAILED"
            ) from exc
        except TalkingHeadProviderError:
            raise
        except Exception as exc:
            raise TalkingHeadProviderError(
                "HeyGen returned an invalid response", code="PROVIDER_RESPONSE_INVALID"
            ) from exc

        if response.status_code in {301, 302, 303, 307, 308}:
            raise TalkingHeadIdentityError("HeyGen redirect rejected")
        if response.status_code >= 400:
            code = (
                "PROVIDER_RATE_LIMITED" if response.status_code == 429
                else "PROVIDER_5XX" if response.status_code >= 500
                else "PROVIDER_4XX"
            )
            raise TalkingHeadProviderError(
                "HeyGen rejected status request",
                code=code,
                status_code=response.status_code,
            )

        data = self._json_object(response).get("data")
        if not isinstance(data, dict):
            raise TalkingHeadProviderError(
                "HeyGen status object missing", code="PROVIDER_JSON_INVALID"
            )
        upstream_status = str(data.get("status", "unknown")).lower()
        status_map = {
            "pending": "pending",
            "processing": "processing",
            "completed": "completed",
            "failed": "failed",
        }
        status = status_map.get(upstream_status, "unknown")
        result: dict = {
            "job_id": job_id,
            "status": status,
            "progress": int(data.get("progress") or 0),
        }
        if status == "completed":
            video_url = data.get("video_url") or data.get("videoUrl")
            if not video_url:
                raise TalkingHeadProviderError(
                    "HeyGen completed job omitted artifact URL",
                    code="ARTIFACT_URL_MISSING",
                )
            self._validated_artifact_url(str(video_url))
            self._artifact_urls[job_id] = str(video_url)
            result["providerArtifactId"] = job_id
        if status == "failed":
            result.update(
                {
                    "errorCode": "PROVIDER_FAILED",
                    "message": "HeyGen reported job failure",
                    "retryable": False,
                }
            )
        return result

    # -------------------------------------------------------------- artifact
    def artifact(self, job_id: str) -> BinaryIO:
        if not self.enabled:
            raise TalkingHeadProviderDisabled()
        self._validate_job_id(job_id)
        source = self._artifact_urls.get(job_id)
        if source is None:
            raise TalkingHeadArtifactError(
                "Artifact identifier is unknown", code="ARTIFACT_ID_UNKNOWN"
            )
        url = self._validated_artifact_url(source)
        output = tempfile.SpooledTemporaryFile(
            max_size=min(self.max_artifact_bytes, 8 * 1024 * 1024)
        )
        total = 0
        try:
            with self._client() as client:
                with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        raise TalkingHeadArtifactError(
                            "Artifact redirect rejected", code="ARTIFACT_REDIRECT_REJECTED"
                        )
                    response.raise_for_status()
                    content_type = response.headers.get(
                        "content-type", ""
                    ).split(";", 1)[0].lower()
                    if content_type != "video/mp4":
                        raise TalkingHeadArtifactError(
                            "Artifact media type rejected", code="ARTIFACT_CONTENT_TYPE_INVALID"
                        )
                    raw_length = response.headers.get("content-length")
                    if raw_length is not None:
                        try:
                            declared_length = int(raw_length)
                        except ValueError as exc:
                            raise TalkingHeadArtifactError(
                                "Artifact length is invalid", code="ARTIFACT_LENGTH_INVALID"
                            ) from exc
                        if declared_length < 0 or declared_length > self.max_artifact_bytes:
                            raise TalkingHeadArtifactError(
                                "Artifact exceeds byte limit", code="ARTIFACT_TOO_LARGE"
                            )
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.max_artifact_bytes:
                            raise TalkingHeadArtifactError(
                                "Artifact exceeds byte limit", code="ARTIFACT_TOO_LARGE"
                            )
                        output.write(chunk)
            if total == 0:
                raise TalkingHeadArtifactError(
                    "Artifact stream is empty", code="ARTIFACT_EMPTY"
                )
            output.seek(0)
            return output
        except TalkingHeadArtifactError:
            output.close()
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            output.close()
            raise TalkingHeadArtifactError(
                "Artifact stream was interrupted", code="ARTIFACT_STREAM_INTERRUPTED"
            ) from exc

    # -------------------------------------------------------------- internal
    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not job_id or any(marker in job_id for marker in ("/", "\\", "..", ":")):
            raise TalkingHeadProviderError(
                "Provider job identifier rejected", code="PROVIDER_ID_INVALID"
            )

    def _validated_artifact_url(self, source: str) -> str:
        if not source or "\\" in source or "\x00" in source:
            raise TalkingHeadArtifactError(
                "Artifact source rejected", code="ARTIFACT_SOURCE_INVALID"
            )
        decoded = unquote(source)
        parsed = urlsplit(decoded)
        if not parsed.scheme or not parsed.hostname:
            raise TalkingHeadArtifactError(
                "Artifact URL rejected", code="ARTIFACT_SOURCE_INVALID"
            )
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or (parsed.scheme, parsed.hostname, parsed.port) not in self._artifact_origins
        ):
            raise TalkingHeadArtifactError(
                "Artifact origin rejected", code="ARTIFACT_ORIGIN_REJECTED"
            )
        path = parsed.path or "/"
        if ".." in PurePosixPath(path).parts:
            raise TalkingHeadArtifactError(
                "Artifact traversal rejected", code="ARTIFACT_PATH_REJECTED"
            )
        query = parsed.query.lower()
        if any(marker in query for marker in ("token", "secret", "key", "signature", "credential")):
            raise TalkingHeadArtifactError(
                "Artifact query rejected", code="ARTIFACT_QUERY_REJECTED"
            )
        return decoded
