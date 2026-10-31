from __future__ import annotations

import asyncio
import hmac
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="G70/G71 FAKE-only fault proxy", docs_url=None, redoc_url=None)

# Fixed internal destination. This service is only added by the disposable
# FAKE-only Compose override and has no published host port.
UPSTREAM = "http://video-use:8002"
TOKEN = os.environ.get("G70_FAULT_PROXY_TOKEN", "")
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "content-length", "content-encoding", "accept-encoding",
}
_state: dict[str, Any] = {
    "armed": False,
    "mode": None,
    "remaining": 0,
    "arm_attempts": 0,
    "arm_forwarded": 0,
    "arm_dropped_success": 0,
    "arm_upstream_statuses": [],
    "arm_media_ids": [],
    "arm_replay_flags": [],
}
_lock = asyncio.Lock()


def require_test_token(request: Request) -> None:
    supplied = request.headers.get("x-g70-fault-token", "")
    if not TOKEN or not hmac.compare_digest(supplied, TOKEN):
        raise HTTPException(status_code=403, detail="FAKE-only test token required")


@app.get("/__health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "service": "g70-fault-proxy", "upstream": UPSTREAM}


@app.post("/__fault/arm")
async def arm(request: Request) -> dict[str, Any]:
    require_test_token(request)
    try:
        payload = await request.json()
        mode = str(payload.get("mode") or "")
        failures = int(payload.get("failures") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Invalid fault plan")
    if mode not in {"fail_before_forward", "forward_then_drop_ack"} or failures != 3:
        raise HTTPException(status_code=422, detail="Only the bounded three-request FAKE-only plans are allowed")
    async with _lock:
        if _state["armed"] or _state["remaining"]:
            raise HTTPException(status_code=409, detail="A fault plan is already active")
        _state.update({
            "armed": True,
            "mode": mode,
            "remaining": failures,
            "arm_attempts": 0,
            "arm_forwarded": 0,
            "arm_dropped_success": 0,
            "arm_upstream_statuses": [],
            "arm_media_ids": [],
            "arm_replay_flags": [],
        })
        return {"armed": True, "mode": mode, "failures": failures}


@app.get("/__fault/stats")
async def stats(request: Request) -> dict[str, Any]:
    require_test_token(request)
    async with _lock:
        return dict(_state)


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def forward(path: str, request: Request) -> Response:
    if path.startswith("__fault/") or path == "__health":
        raise HTTPException(status_code=404, detail="Not found")

    method = request.method.upper()
    is_media_post = method == "POST" and path == "media"
    fault_mode: str | None = None
    if is_media_post:
        async with _lock:
            if _state["armed"] and _state["remaining"] > 0:
                fault_mode = str(_state["mode"])
                _state["remaining"] -= 1
                _state["arm_attempts"] += 1
                if _state["remaining"] == 0:
                    _state["armed"] = False

    body = await request.body()
    if fault_mode == "fail_before_forward":
        return JSONResponse(
            status_code=503,
            content={"detail": "FAKE-only injected pre-storage failure"},
        )

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    try:
        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            upstream_response = await client.request(
                method,
                f"{UPSTREAM}/{path}",
                params=request.query_params,
                content=body,
                headers=headers,
            )
    except httpx.HTTPError:
        return JSONResponse(
            status_code=502,
            content={"detail": "FAKE-only proxy could not reach the disposable Sidecar"},
        )

    if is_media_post and fault_mode == "forward_then_drop_ack":
        media_id = None
        replay_flag = None
        if upstream_response.status_code == 201:
            try:
                payload = upstream_response.json()
                media_id = payload.get("mediaId")
                replay_flag = bool(payload.get("idempotentReplay", False))
            except (TypeError, ValueError):
                media_id = None
        async with _lock:
            _state["arm_forwarded"] += 1
            _state["arm_upstream_statuses"].append(upstream_response.status_code)
            if upstream_response.status_code == 201:
                _state["arm_dropped_success"] += 1
            if media_id:
                _state["arm_media_ids"].append(str(media_id))
            if replay_flag is not None:
                _state["arm_replay_flags"].append(replay_flag)
        return JSONResponse(
            status_code=503,
            content={"detail": "FAKE-only injected lost Sidecar acknowledgement"},
        )

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() in {"content-type", "content-disposition", "cache-control", "etag", "last-modified"}
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )
