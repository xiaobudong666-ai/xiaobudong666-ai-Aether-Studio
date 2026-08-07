from __future__ import annotations

import os
import time
from typing import Any

import httpx


class VideoUseError(RuntimeError):
    """Base exception for the isolated video-use sidecar."""


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

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                    response = client.request(method, f"{self.api_url}{path}", **kwargs)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise VideoUseError("video-use returned a non-object response")
                return payload
            except (httpx.HTTPError, ValueError, VideoUseError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                time.sleep(self.backoff_factor * (2**attempt))
        raise VideoUseError(f"video-use request failed: {last_error}") from last_error

    def check_health(self) -> dict[str, Any]:
        try:
            return self._request("GET", "/health")
        except VideoUseError as exc:
            return {"status": "unhealthy", "reason": str(exc)}

    def get_capabilities(self) -> dict[str, Any]:
        return self._request("GET", "/capabilities")

    def submit_render(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/renders", json=payload)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")
