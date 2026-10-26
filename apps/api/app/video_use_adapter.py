from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Any, BinaryIO, Iterator

import httpx


class VideoUseError(RuntimeError):
    """Base exception for the isolated video-use sidecar."""


class VideoUseConnectionError(VideoUseError):
    pass


class VideoUseTimeoutError(VideoUseError):
    pass


class VideoUseAdapter:
    def __init__(
        self,
        api_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_factor: float = 0.25,
    ):
        self.api_url = (api_url or os.environ.get("VIDEO_USE_API_URL", "http://video-use:8002")).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        # POST /media is not idempotent: the sidecar may have persisted bytes
        # before a timeout or 5xx. A blind HTTP retry creates another object
        # and may replay an already-consumed binary stream. Reconcile via the
        # original durable upload intent instead of issuing a second upload.
        retries = 0 if method.upper() == "POST" and path == "/media" else self.max_retries
        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                    response = client.request(method, f"{self.api_url}{path}", **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                return response
            except httpx.TimeoutException as exc:
                last_error = VideoUseTimeoutError(
                    f"video-use request timed out after {self.timeout}s"
                )
            except httpx.HTTPError as exc:
                last_error = VideoUseConnectionError(f"video-use request failed: {exc}")
            if attempt < retries:
                time.sleep(self.backoff_factor * (2**attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VideoUseError(f"Invalid video-use response: {exc}") from exc
        if not isinstance(payload, dict):
            raise VideoUseError("video-use returned a non-object response")
        return payload

    def check_health(self) -> dict[str, Any]:
        try:
            return self._json(self._request("GET", "/health"))
        except VideoUseError as exc:
            return {"status": "unhealthy", "reason": str(exc)}

    def get_capabilities(self) -> dict[str, Any]:
        return self._json(self._request("GET", "/capabilities"))

    def upload_media(
        self,
        project_id: str,
        filename: str,
        content_type: str | None,
        stream: BinaryIO,
        *,
        recovery_key: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        form = {"projectId": project_id}
        if recovery_key is not None or expected_sha256 is not None:
            if not recovery_key or not expected_sha256:
                raise ValueError("Incomplete FAKE media recovery intent")
            form.update(recoveryKey=recovery_key, expectedSha256=expected_sha256)
        return self._json(
            self._request(
                "POST",
                "/media",
                data=form,
                files={"file": (filename, stream, content_type or "application/octet-stream")},
            )
        )

    def find_fake_recovery(
        self, project_id: str, recovery_key: str, expected_sha256: str,
    ) -> dict[str, Any]:
        # Only reads back the original scoped sidecar object; never resubmits
        # Provider generation or retries the ambiguous media POST.
        return self._json(self._request(
            "GET",
            f"/fake-recovery/{project_id}/{recovery_key}",
            params={"expectedSha256": expected_sha256},
        ))

    def find_fake_recovery_optional(
        self, project_id: str, recovery_key: str, expected_sha256: str,
    ) -> dict[str, Any] | None:
        """Read a scoped FAKE recovery object, returning None only on explicit 404.

        Transport errors, 5xx, disabled recovery and hash conflicts are not
        interpreted as absence; callers must fail closed on ambiguous lookup.
        """
        response = self._request(
            "GET",
            f"/fake-recovery/{project_id}/{recovery_key}",
            params={"expectedSha256": expected_sha256},
        )
        if response.status_code == 404:
            return None
        return self._json(response)

    def submit_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._request("POST", "/renders", json=payload))

    def submit_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._request("POST", "/transcriptions", json=payload))

    def submit_timeline_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._json(self._request("POST", "/timeline-views", json=payload))

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._json(self._request("GET", f"/jobs/{job_id}"))

    @contextmanager
    def stream(
        self,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> Iterator[httpx.Response]:
        try:
            with httpx.Client(timeout=None, trust_env=False) as client:
                with client.stream(
                    "GET",
                    f"{self.api_url}{path}",
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    yield response
        except httpx.TimeoutException as exc:
            raise VideoUseTimeoutError("video-use media stream timed out") from exc
        except httpx.HTTPError as exc:
            raise VideoUseConnectionError(f"video-use media stream failed: {exc}") from exc
