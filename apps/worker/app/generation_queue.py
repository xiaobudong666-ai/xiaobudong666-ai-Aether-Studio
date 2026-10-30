from __future__ import annotations

import os
import socket
from typing import Any, BinaryIO

import httpx


class GenerationQueueError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class GenerationQueueClient:
    def __init__(
        self,
        backend_url: str | None = None,
        worker_token: str | None = None,
        worker_id: str | None = None,
        timeout: float = 30.0,
    ):
        self.backend_url = (backend_url or os.environ.get("BACKEND_URL", "http://api:8000")).rstrip("/")
        self.worker_token = worker_token if worker_token is not None else os.environ.get("AETHER_WORKER_TOKEN", "")
        self.worker_id = worker_id or os.environ.get("AETHER_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")
        self.timeout = timeout
        self._lease_epochs: dict[str, int] = {}

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Worker-Token": self.worker_token, "X-Worker-Id": self.worker_id}

    def _request(
        self, method: str, path: str, *, extra_headers: dict[str, str] | None = None, **kwargs: Any
    ) -> httpx.Response:
        if not self.worker_token:
            raise GenerationQueueError("AETHER_WORKER_TOKEN is not configured")
        request_headers = self.headers
        if extra_headers:
            request_headers.update(extra_headers)
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                response = client.request(method, f"{self.backend_url}{path}", headers=request_headers, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            code = None
            try:
                detail = exc.response.json().get("detail")
                if isinstance(detail, dict):
                    code = detail.get("code")
            except (TypeError, ValueError):
                pass
            raise GenerationQueueError(
                "Generation queue rejected the request",
                status_code=exc.response.status_code,
                code=str(code) if code else None,
            ) from exc
        except httpx.HTTPError as exc:
            raise GenerationQueueError("Generation queue request failed") from exc

    def claim(self) -> dict[str, Any] | None:
        response = self._request("POST", "/internal/generation-tasks/claim")
        if response.status_code == 204:
            return None
        payload = response.json()
        if not isinstance(payload, dict):
            raise GenerationQueueError("Generation queue returned a non-object claim")
        task_id = str(payload.get("taskId") or "")
        lease_epoch = payload.get("leaseEpoch")
        if not task_id or not isinstance(lease_epoch, int) or lease_epoch < 1:
            raise GenerationQueueError("Generation queue returned a claim without a valid lease epoch")
        self._lease_epochs[task_id] = lease_epoch
        return payload

    def attest(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/internal/generation/providers/moneyprinter/attest",
            json=payload,
        ).json()

    def _lease_headers(self, task_id: str) -> dict[str, str]:
        epoch = self._lease_epochs.get(task_id)
        if epoch is None:
            raise GenerationQueueError("No current lease epoch is available for this task")
        return {"X-Worker-Lease-Epoch": str(epoch)}

    def heartbeat(self, task_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/internal/generation-tasks/{task_id}/heartbeat",
            extra_headers=self._lease_headers(task_id), json={}
        ).json()

    def transition(
        self,
        task_id: str,
        *,
        status: str,
        progress: int,
        message: str,
        upstream_job_id: str | None = None,
        provider_artifact_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/generation-tasks/{task_id}/transition",
            extra_headers=self._lease_headers(task_id),
            json={
                "status": status,
                "progress": progress,
                "message": message,
                "upstreamJobId": upstream_job_id,
                "providerArtifactId": provider_artifact_id,
                "errorCode": error_code,
                "errorMessage": error_message,
                "retryable": retryable,
            },
        ).json()

    def artifact_intake(
        self,
        task_id: str,
        provider_artifact_id: str,
        stream: BinaryIO,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            extra_headers=self._lease_headers(task_id),
            data={"providerArtifactId": provider_artifact_id},
            files={"file": ("generated.mp4", stream, "video/mp4")},
        ).json()
