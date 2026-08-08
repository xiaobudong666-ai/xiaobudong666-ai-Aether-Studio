from __future__ import annotations

import os
import socket
from typing import Any

import httpx


class TaskQueueError(RuntimeError):
    pass


class TaskQueueClient:
    def __init__(
        self,
        backend_url: str | None = None,
        worker_token: str | None = None,
        worker_id: str | None = None,
        timeout: float = 15.0,
    ):
        self.backend_url = (backend_url or os.environ.get("BACKEND_URL", "http://api:8000")).rstrip("/")
        self.worker_token = worker_token if worker_token is not None else os.environ.get("AETHER_WORKER_TOKEN", "")
        self.worker_id = worker_id or os.environ.get("AETHER_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
        self.timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Worker-Token": self.worker_token, "X-Worker-Id": self.worker_id}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.worker_token:
            raise TaskQueueError("AETHER_WORKER_TOKEN is not configured")
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.request(method, f"{self.backend_url}{path}", headers=self.headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise TaskQueueError(f"Task queue request failed: {exc}") from exc

    def recover(self) -> list[str]:
        payload = self._request("POST", "/internal/render-tasks/recover").json()
        return list(payload.get("recoveredTaskIds", []))

    def claim(self) -> dict[str, Any] | None:
        response = self._request("POST", "/internal/render-tasks/claim")
        if response.status_code == 204:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            raise TaskQueueError("Task queue returned a non-object claim")
        return payload

    def update(
        self,
        task_id: str,
        *,
        status: str,
        progress: int,
        message: str,
        upstream_job_id: str | None = None,
        error: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "status": status,
            "progress": progress,
            "message": message,
            "upstreamJobId": upstream_job_id,
            "error": error,
            "retryable": retryable,
        }
        return self._request("POST", f"/internal/render-tasks/{task_id}/update", json=payload).json()
