from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.parse import unquote, urljoin, urlsplit

import httpx

logger = logging.getLogger("worker.moneyprinter_adapter")

ADAPTER_VERSION = "aether-moneyprinter-v2"
UPSTREAM_VERSION = "v1.2.7"
UPSTREAM_PIN = "475f21147f0808f5ffe3f58af9ab794b28a4da2c"


class MoneyPrinterError(Exception):
    """Sanitized, stable Adapter error boundary."""

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


class MoneyPrinterTimeoutError(MoneyPrinterError):
    pass


class MoneyPrinterConnectionError(MoneyPrinterError):
    pass


class MoneyPrinterAmbiguousSubmissionError(MoneyPrinterError):
    pass


class MoneyPrinterTaskFailedError(MoneyPrinterError):
    pass


class MoneyPrinterArtifactError(MoneyPrinterError):
    pass


class MoneyPrinterTurboAdapter:
    """The Worker-only, deny-by-default boundary to the pinned Sidecar."""

    def __init__(
        self,
        api_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_factor: float | None = None,
        degrade_on_failure: bool = False,
        artifact_path_prefixes: list[str] | None = None,
        max_artifact_bytes: int | None = None,
    ):
        raw_url = api_url or os.environ.get(
            "MONEYPRINTER_API_URL", "http://moneyprinter:8080"
        )
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MoneyPrinter Sidecar origin is invalid")
        self.api_url = raw_url.rstrip("/")
        self._origin = (parsed.scheme, parsed.hostname, parsed.port)
        self.timeout = float(timeout or os.environ.get("MONEYPRINTER_TIMEOUT", "10.0"))
        self.max_retries = max(1, int(max_retries or os.environ.get("MONEYPRINTER_MAX_RETRIES", "3")))
        self.backoff_factor = float(backoff_factor or os.environ.get("MONEYPRINTER_RETRY_BACKOFF", "2.0"))
        self.degrade_on_failure = degrade_on_failure
        self.artifact_path_prefixes = tuple(
            artifact_path_prefixes or ["/artifacts/", "/api/v1/artifacts/"]
        )
        self.max_artifact_bytes = int(
            max_artifact_bytes
            or os.environ.get("AETHER_GENERATION_MAX_ARTIFACT_BYTES", str(2 * 1024**3))
        )
        self._artifact_sources: dict[str, str] = {}
        logger.info(
            "MoneyPrinter Adapter initialized (version=%s, upstream_pin=%s, retries=%s)",
            ADAPTER_VERSION,
            UPSTREAM_PIN[:12],
            self.max_retries,
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
        )

    def _request_with_retry(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        if not path.startswith("/") or ".." in PurePosixPath(unquote(path)).parts:
            raise MoneyPrinterError("Provider path rejected", code="PROVIDER_PATH_INVALID")
        url = f"{self.api_url}{path}"
        for attempt in range(1, self.max_retries + 1):
            try:
                with self._client() as client:
                    response = client.request(
                        method.upper(), url, json=json_data, params=params
                    )
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise MoneyPrinterError(
                        "Provider redirect rejected",
                        code="PROVIDER_REDIRECT_REJECTED",
                        status_code=response.status_code,
                    )
                response.raise_for_status()
                return response
            except httpx.ReadTimeout as exc:
                if method.upper() == "POST":
                    raise MoneyPrinterAmbiguousSubmissionError(
                        "Provider submission outcome is unknown",
                        code="AMBIGUOUS_SUBMISSION",
                    ) from exc
                if attempt >= self.max_retries:
                    raise MoneyPrinterTimeoutError(
                        "Provider response timed out",
                        code="PROVIDER_READ_TIMEOUT",
                        retryable=True,
                    ) from exc
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt >= self.max_retries:
                    raise MoneyPrinterConnectionError(
                        "Provider connection failed",
                        code="PROVIDER_CONNECTION_FAILED",
                        retryable=True,
                    ) from exc
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                retryable = status_code == 429 or status_code >= 500
                if retryable and attempt < self.max_retries:
                    time.sleep(self.backoff_factor ** attempt)
                    continue
                raise MoneyPrinterError(
                    "Provider rejected the request",
                    code=("PROVIDER_RATE_LIMITED" if status_code == 429 else
                          "PROVIDER_5XX" if status_code >= 500 else "PROVIDER_4XX"),
                    status_code=status_code,
                    retryable=retryable,
                ) from exc
            except MoneyPrinterError:
                raise
            except Exception as exc:
                raise MoneyPrinterError(
                    "Provider returned an invalid response",
                    code="PROVIDER_RESPONSE_INVALID",
                ) from exc
            if attempt < self.max_retries:
                time.sleep(self.backoff_factor ** attempt)
        raise MoneyPrinterError("Provider request failed", code="PROVIDER_ERROR")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except Exception as exc:
            raise MoneyPrinterError(
                "Provider returned invalid JSON", code="PROVIDER_JSON_INVALID"
            ) from exc
        if not isinstance(payload, dict):
            raise MoneyPrinterError(
                "Provider returned an invalid object", code="PROVIDER_JSON_INVALID"
            )
        return payload

    def check_health(self) -> dict:
        try:
            self._request_with_retry("GET", "/openapi.json")
            return {
                "status": "healthy",
                "service": "moneyprinter-sidecar",
                "responsive": True,
                "adapterVersion": ADAPTER_VERSION,
                "upstreamPin": UPSTREAM_PIN,
            }
        except Exception:
            return self.degrade() if self.degrade_on_failure else {
                "status": "unhealthy",
                "service": "moneyprinter-sidecar",
                "responsive": False,
                "reasonCode": "PROVIDER_HEALTH_FAILED",
                "adapterVersion": ADAPTER_VERSION,
                "upstreamPin": UPSTREAM_PIN,
            }

    def get_capabilities(self) -> dict:
        health = self.check_health()
        healthy = health["status"] == "healthy"
        return {
            "status": "active" if healthy else "degraded",
            "healthy": healthy,
            "adapterVersion": ADAPTER_VERSION,
            "upstreamPin": UPSTREAM_PIN,
            "capabilities": {
                "videoAspects": ["9:16", "16:9", "1:1"] if healthy else [],
                "videoConcatModes": ["random", "sequential"] if healthy else [],
                "maxOutputs": 1,
                "maxClipDurationSeconds": 10,
                "cancellationSupported": False,
                "artifactStreaming": True,
            },
        }

    def generate_video(
        self,
        subject: str,
        aspect: str = "9:16",
        voice_name: str = "en-US-JennyNeural",
        video_concat_mode: str = "random",
        video_clip_duration: int = 5,
    ) -> str:
        if not subject.strip() or len(subject) > 500:
            raise MoneyPrinterError("Generation subject rejected", code="PROVIDER_REQUEST_INVALID", status_code=422)
        if aspect not in {"9:16", "16:9", "1:1"}:
            raise MoneyPrinterError("Generation aspect rejected", code="PROVIDER_REQUEST_INVALID", status_code=422)
        if video_concat_mode not in {"random", "sequential"} or not 1 <= int(video_clip_duration) <= 10:
            raise MoneyPrinterError("Generation options rejected", code="PROVIDER_REQUEST_INVALID", status_code=422)
        payload = {
            "video_subject": subject,
            "video_aspect": aspect,
            "voice_name": voice_name,
            "video_concat_mode": video_concat_mode,
            "video_clip_duration": int(video_clip_duration),
        }
        response = self._request_with_retry("POST", "/api/v1/videos", json_data=payload)
        data = self._json_object(response).get("data")
        if not isinstance(data, dict) or not (data.get("task_id") or data.get("taskId")):
            raise MoneyPrinterError("Provider omitted task identifier", code="PROVIDER_TASK_ID_MISSING")
        return str(data.get("task_id") or data.get("taskId"))

    def get_task_status(self, task_id: str) -> dict:
        if not task_id or any(marker in task_id for marker in ("/", "\\", "..", ":")):
            raise MoneyPrinterError("Provider task identifier rejected", code="PROVIDER_TASK_ID_INVALID")
        response = self._request_with_retry("GET", f"/api/v1/tasks/{task_id}")
        data = self._json_object(response).get("data")
        if not isinstance(data, dict):
            raise MoneyPrinterError("Provider status object missing", code="PROVIDER_JSON_INVALID")
        state = data.get("state")
        status_map = {
            -1: "failed", 0: "queued", 1: "completed", 2: "queued",
            3: "processing", 4: "processing", 5: "canceled",
        }
        status = status_map.get(state, "unknown")
        result = {
            "task_id": task_id,
            "status": status,
            "progress": max(0, min(100, int(data.get("progress") or 0))),
        }
        if status == "failed":
            result.update({"errorCode": "PROVIDER_FAILED", "message": "Provider reported failure", "retryable": False})
        combined = data.get("combined_videos")
        if status == "completed" and isinstance(combined, list) and combined:
            source = str(combined[0])
            self._validated_artifact_url(source)
            artifact_id = hashlib.sha256(f"{task_id}:{source}".encode("utf-8")).hexdigest()[:40]
            self._artifact_sources[artifact_id] = source
            result["providerArtifactId"] = artifact_id
        return result

    def cancel_task(self, _task_id: str) -> None:
        raise MoneyPrinterError(
            "Provider cancellation is not supported",
            code="PROVIDER_CANCEL_UNSUPPORTED",
        )

    def _validated_artifact_url(self, source: str) -> str:
        if not source or "\\" in source or "\x00" in source:
            raise MoneyPrinterArtifactError("Artifact source rejected", code="ARTIFACT_SOURCE_INVALID")
        decoded = unquote(source)
        parsed = urlsplit(decoded)
        if parsed.scheme:
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.username is not None
                or parsed.password is not None
                or (parsed.scheme, parsed.hostname, parsed.port) != self._origin
            ):
                raise MoneyPrinterArtifactError("Artifact origin rejected", code="ARTIFACT_ORIGIN_REJECTED")
            path = parsed.path
        else:
            if not decoded.startswith("/") or parsed.netloc:
                raise MoneyPrinterArtifactError("Artifact path rejected", code="ARTIFACT_PATH_REJECTED")
            path = parsed.path
        if ".." in PurePosixPath(path).parts:
            raise MoneyPrinterArtifactError("Artifact traversal rejected", code="ARTIFACT_PATH_REJECTED")
        if not any(path.startswith(prefix) for prefix in self.artifact_path_prefixes):
            raise MoneyPrinterArtifactError("Artifact prefix rejected", code="ARTIFACT_PATH_REJECTED")
        query = parsed.query.lower()
        if any(marker in query for marker in ("token", "secret", "key", "signature", "credential")):
            raise MoneyPrinterArtifactError("Artifact query rejected", code="ARTIFACT_QUERY_REJECTED")
        return urljoin(f"{self.api_url}/", decoded)

    def stream_artifact(self, provider_artifact_id: str) -> BinaryIO:
        source = self._artifact_sources.get(provider_artifact_id)
        if source is None:
            raise MoneyPrinterArtifactError("Artifact identifier is unknown", code="ARTIFACT_ID_UNKNOWN")
        url = self._validated_artifact_url(source)
        output = tempfile.SpooledTemporaryFile(max_size=min(self.max_artifact_bytes, 8 * 1024**2))
        total = 0
        try:
            with self._client() as client:
                with client.stream("GET", url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        raise MoneyPrinterArtifactError("Artifact redirect rejected", code="ARTIFACT_REDIRECT_REJECTED")
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    if content_type != "video/mp4":
                        raise MoneyPrinterArtifactError("Artifact media type rejected", code="ARTIFACT_CONTENT_TYPE_INVALID")
                    raw_length = response.headers.get("content-length")
                    if raw_length is not None:
                        try:
                            declared_length = int(raw_length)
                        except ValueError as exc:
                            raise MoneyPrinterArtifactError(
                                "Artifact length is invalid", code="ARTIFACT_LENGTH_INVALID"
                            ) from exc
                        if declared_length < 0 or declared_length > self.max_artifact_bytes:
                            raise MoneyPrinterArtifactError("Artifact exceeds byte limit", code="ARTIFACT_TOO_LARGE")
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.max_artifact_bytes:
                            raise MoneyPrinterArtifactError("Artifact exceeds byte limit", code="ARTIFACT_TOO_LARGE")
                        output.write(chunk)
            if total == 0:
                raise MoneyPrinterArtifactError("Artifact stream is empty", code="ARTIFACT_EMPTY")
            output.seek(0)
            return output
        except MoneyPrinterArtifactError:
            output.close()
            raise
        except (httpx.HTTPError, OSError, ValueError) as exc:
            output.close()
            raise MoneyPrinterArtifactError(
                "Artifact stream was interrupted",
                code="ARTIFACT_STREAM_INTERRUPTED",
                retryable=True,
            ) from exc

    def degrade(self) -> dict:
        return {
            "status": "degraded",
            "service": "moneyprinter-sidecar",
            "responsive": False,
            "fallback_active": True,
            "reasonCode": "PROVIDER_HEALTH_FAILED",
            "adapterVersion": ADAPTER_VERSION,
            "upstreamPin": UPSTREAM_PIN,
            "capabilities": {
                "videoAspects": [],
                "videoConcatModes": [],
                "maxOutputs": 1,
                "maxClipDurationSeconds": 10,
                "cancellationSupported": False,
                "artifactStreaming": False,
            },
        }
