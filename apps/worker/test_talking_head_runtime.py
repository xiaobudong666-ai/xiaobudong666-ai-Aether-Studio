"""A1-P2: Talking-head Worker Runtime wiring (governed, fake-only).

These tests prove that the already-merged ``TalkingHeadProviderAdapter /
HeyGenTalkingHeadAdapter`` is minimally wired into the Worker provider routing
and initialization path:

* default-off (no Provider network, never reads HEYGEN_API_KEY);
* 1 task / 1 submission / 1 attempt / 0 retry / 0 fallback;
* the completed artifact re-enters ``artifact_intake`` so it continues through
  Aether Asset / Rights / Timeline / Render / Detection governance and is never
  published by itself.

Every request is served by ``httpx.MockTransport``; no real HeyGen request is
ever issued.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from app.generation_queue import GenerationQueueError
from app.main import (
    WorkerComponents,
    initialize_worker,
    process_generation_task,
)
from app.talking_head_provider_adapter import HeyGenTalkingHeadAdapter


def golden_transport():
    def handler(request):
        url = str(request.url)
        if url == "https://api.heygen.com/v3/videos" and request.method == "POST":
            return httpx.Response(
                200, json={"data": {"video_id": "video-1"}}, request=request
            )
        if url == "https://api.heygen.com/v3/videos/video-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "video_id": "video-1",
                        "status": "completed",
                        "video_url": "https://cdn.heygen.com/videos/video-1.mp4",
                        "duration": 10,
                    }
                },
                request=request,
            )
        if url == "https://cdn.heygen.com/videos/video-1.mp4":
            return httpx.Response(
                200,
                content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
                headers={"content-type": "video/mp4", "content-length": "28"},
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler)


def enabled_adapter(**kwargs):
    return HeyGenTalkingHeadAdapter(
        enabled=True, transport=golden_transport(), **kwargs
    )


class RecordingQueue:
    def __init__(self):
        self.transitions = []
        self.heartbeats = 0
        self.intakes = []

    def attest(self, payload):
        self.attested = payload
        return {"ok": True}

    def heartbeat(self, task_id):
        self.heartbeats += 1
        return {"leaseExpiresAt": "later"}

    def transition(self, task_id, **values):
        self.transitions.append((task_id, dict(values)))
        return {"taskId": task_id, **values}

    def artifact_intake(self, task_id, provider_artifact_id, stream):
        data = stream.read() if hasattr(stream, "read") else bytes(stream)
        self.intakes.append((task_id, provider_artifact_id, data))
        return {"taskId": task_id, "status": "RIGHTS_BLOCKED"}


def _submits(calls):
    return [c for c in calls if c.method == "POST"]


class PersistenceFailQueue(RecordingQueue):
    """Fails the first RUNNING transition to simulate a post-submit write loss."""

    def __init__(self):
        super().__init__()
        self._fail_run = True

    def transition(self, task_id, **values):
        if self._fail_run and values.get("status") == "RUNNING":
            self._fail_run = False
            raise GenerationQueueError(
                "Generation queue unavailable", code="GENERATION_QUEUE_UNAVAILABLE"
            )
        return super().transition(task_id, **values)


class TotalPersistenceFailQueue(RecordingQueue):
    """Fails both the RUNNING write and the follow-up fail-closed UNKNOWN write."""

    def __init__(self):
        super().__init__()
        self._fail_run = True

    def transition(self, task_id, **values):
        if self._fail_run and values.get("status") == "RUNNING":
            self._fail_run = False
            raise GenerationQueueError(
                "Generation queue unavailable", code="GENERATION_QUEUE_UNAVAILABLE"
            )
        if values.get("status") == "UNKNOWN":
            raise GenerationQueueError(
                "Generation queue unavailable", code="GENERATION_QUEUE_UNAVAILABLE"
            )
        return super().transition(task_id, **values)


def components(talking_head, queue):
    return WorkerComponents(
        ffmpeg=None, ai=None, recovery=None,
        moneyprinter=None, video_use=None,
        generation_queue=queue, talking_head=talking_head,
    )


def claimed_talking_head_task(**overrides):
    task = {
        "taskId": "talking-head-1",
        "projectId": "project-1",
        "attempt": 1,
        "providerMode": "talking-head",
        "upstreamJobId": None,
        "submissionConsumed": False,
        "request": {
            "text": "大家好，这是一条十秒口播测试。",
            "durationSeconds": 10,
            "aspectRatio": "9:16",
            "resolution": "1080p",
            "voiceId": "voice-zh-CN-1",
            "imageUrl": "https://static.example/photo.jpg",
        },
    }
    task.update(overrides)
    return task


# ------------------------------------------------------------- default-off
def test_initialize_worker_wires_talking_head_default_disabled(monkeypatch):
    monkeypatch.delenv("AETHER_GENERATION_PROVIDER_MODE", raising=False)
    comps = initialize_worker()
    assert isinstance(comps.talking_head, HeyGenTalkingHeadAdapter)
    assert comps.talking_head.enabled is False
    assert comps.talking_head.check_health()["status"] == "disabled"


def test_disabled_talking_head_runtime_fails_closed_without_submit():
    adapter = HeyGenTalkingHeadAdapter()  # default-off, no transport
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "PROVIDER_DISABLED"
    assert result["retryable"] is False
    # No RUNNING/INGESTING transition and no submission attempt was made.
    assert [t[1]["status"] for t in queue.transitions] == ["FAILED"]
    assert queue.intakes == []


# --------------------------------------------------- golden chain, 0 retry
def test_talking_head_golden_chain_single_attempt_and_artifact_intake():
    queue = RecordingQueue()
    result = process_generation_task(
        components(enabled_adapter(), queue),
        claimed_talking_head_task(),
        poll_interval=0,
    )

    assert result["status"] == "RIGHTS_BLOCKED"
    statuses = [t[1]["status"] for t in queue.transitions]
    assert statuses == ["RUNNING", "INGESTING"]
    # 1 submission / 1 attempt / 0 retry / 0 fallback.
    assert all(not t[1].get("retryable") for t in queue.transitions)
    assert queue.heartbeats == 1
    assert len(queue.intakes) == 1

    task_id, provider_artifact_id, payload = queue.intakes[0]
    assert task_id == "talking-head-1"
    assert provider_artifact_id == "video-1"
    assert payload == b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes"


def test_talking_head_runtime_maps_request_to_job_spec_and_pins_9_16():
    captured = {}

    def handler(request):
        if request.method == "POST":
            captured["payload"] = json.loads(request.content)
            return httpx.Response(
                200, json={"data": {"video_id": "video-1"}}, request=request
            )
        if request.method == "GET" and str(request.url) == "https://cdn.heygen.com/videos/video-1.mp4":
            return httpx.Response(
                200,
                content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
                headers={"content-type": "video/mp4", "content-length": "28"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "video_id": "video-1",
                    "status": "completed",
                    "video_url": "https://cdn.heygen.com/videos/video-1.mp4",
                }
            },
            request=request,
        )

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue),
        claimed_talking_head_task(
            request={
                "text": "竖屏口播",
                "durationSeconds": 8,
                "voiceId": "voice-zh-CN-2",
                "avatarId": "avatar-42",
            },
        ),
        poll_interval=0,
    )
    # The submit payload must prove 9:16/1080p pinning and avatar-id routing.
    assert captured["payload"]["aspect_ratio"] == "9:16"
    assert captured["payload"]["resolution"] == "1080p"
    assert captured["payload"]["avatar_id"] == "avatar-42"
    assert captured["payload"]["script"] == "竖屏口播"
    assert captured["payload"]["voice_id"] == "voice-zh-CN-2"
    assert result["status"] == "RIGHTS_BLOCKED"


# ------------------------------------------------------- fail-closed states
def test_ambiguous_submission_is_unknown_without_repost():
    def handler(request):
        raise httpx.ReadTimeout("lost response", request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "AMBIGUOUS_SUBMISSION"
    assert result["retryable"] is False
    assert [t[1]["status"] for t in queue.transitions] == ["UNKNOWN"]
    assert queue.intakes == []


def test_cost_ceiling_fail_closed_without_any_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"video_id": "v1"}}, request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True,
        transport=httpx.MockTransport(handler),
        cost_ceiling_usd=0.50,
        cost_per_second_usd=0.10,
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "COST_CEILING_EXCEEDED"
    assert calls == []


def test_status_unknown_stops_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)
        return httpx.Response(
            200, json={"data": {"video_id": "video-1", "status": "weird"}}, request=request
        )

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "PROVIDER_STATUS_UNKNOWN"
    assert result["retryable"] is False
    assert len(_submits(calls)) == 1


def test_status_failed_is_non_retryable():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)
        return httpx.Response(
            200,
            json={"data": {"video_id": "video-1", "status": "failed"}},
            request=request,
        )

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "PROVIDER_FAILED"
    assert result["retryable"] is False
    assert len(_submits(calls)) == 1


def test_invalid_job_spec_fails_closed_before_submit():
    queue = RecordingQueue()
    result = process_generation_task(
        components(enabled_adapter(), queue),
        claimed_talking_head_task(
            request={"text": "bad", "durationSeconds": 11, "voiceId": "v"}
        ),
        poll_interval=0,
    )
    assert result["status"] == "FAILED"
    assert result["error_code"] == "JOB_SPEC_INVALID"
    assert [t[1]["status"] for t in queue.transitions] == ["FAILED"]
    assert queue.intakes == []


# ---------------------------------------------- at-most-once submission proofs
def test_talking_head_golden_chain_submits_exactly_once():
    calls = []

    def handler(request):
        calls.append(request)
        url = str(request.url)
        if url == "https://api.heygen.com/v3/videos" and request.method == "POST":
            return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)
        if url == "https://api.heygen.com/v3/videos/video-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "video_id": "video-1",
                        "status": "completed",
                        "video_url": "https://cdn.heygen.com/videos/video-1.mp4",
                    }
                },
                request=request,
            )
        if url == "https://cdn.heygen.com/videos/video-1.mp4":
            return httpx.Response(
                200,
                content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
                headers={"content-type": "video/mp4", "content-length": "28"},
                request=request,
            )
        return httpx.Response(404, request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "RIGHTS_BLOCKED"
    assert len(_submits(calls)) == 1
    assert len(queue.intakes) == 1


def test_ambiguous_submission_submits_exactly_once():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "AMBIGUOUS_SUBMISSION"
    assert len(_submits(calls)) == 1


def test_running_persistence_failure_fails_closed():
    adapter = enabled_adapter()
    queue = PersistenceFailQueue()
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "SUBMISSION_PERSISTENCE_FAILED"
    # Terminal UNKNOWN still records the upstream id so a future claim can
    # resume status polling instead of re-submitting.
    assert any(
        t[1].get("status") == "UNKNOWN" and t[1].get("upstream_job_id") == "video-1"
        for t in queue.transitions
    )


def test_worker_restart_never_resubmits_consumed_attempt():
    """Cross-restart at-most-once proof.

    Worker A consumes the single submission right and then loses both the
    RUNNING write and the fail-closed UNKNOWN write.  Worker B is a brand-new
    ``WorkerComponents`` instance (no shared Python memory) and receives a
    claim whose persisted state derives ``submissionConsumed=True``.  It must
    fail closed with 0 additional submits, so the total stays exactly 1.
    """
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "video_id": "video-1",
                    "status": "completed",
                    "video_url": "https://cdn.heygen.com/videos/video-1.mp4",
                }
            },
            request=request,
        )

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )

    # Worker A: fresh claim, submit succeeds, then every persistence write fails.
    worker_a = components(adapter, TotalPersistenceFailQueue())
    first = process_generation_task(
        worker_a, claimed_talking_head_task(), poll_interval=0
    )
    assert first["status"] == "UNKNOWN"
    assert first["errorCode"] == "GENERATION_QUEUE_UNAVAILABLE"
    assert len(_submits(calls)) == 1

    # Worker A is destroyed; Worker B is a fresh instance with the same
    # logical task/attempt and a claim carrying the persisted consumed state.
    worker_b = components(adapter, RecordingQueue())
    second = process_generation_task(
        worker_b,
        claimed_talking_head_task(submissionConsumed=True),
        poll_interval=0,
    )
    assert second["status"] == "UNKNOWN"
    assert second["error_code"] == "SUBMISSION_PERSISTENCE_FAILED"
    # No second submission ever reaches the Provider.
    assert len(_submits(calls)) == 1


def test_consumed_claim_fails_closed_without_any_submit():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    result = process_generation_task(
        components(adapter, queue),
        claimed_talking_head_task(submissionConsumed=True),
        poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "SUBMISSION_PERSISTENCE_FAILED"
    assert result["retryable"] is False
    assert calls == []
    assert [t[1]["status"] for t in queue.transitions] == ["UNKNOWN"]
    assert queue.intakes == []


def test_existing_upstream_job_id_skips_submit():
    calls = []

    def handler(request):
        calls.append(request)
        url = str(request.url)
        if url == "https://api.heygen.com/v3/videos/video-1" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "video_id": "video-1",
                        "status": "completed",
                        "video_url": "https://cdn.heygen.com/videos/video-1.mp4",
                    }
                },
                request=request,
            )
        if url == "https://cdn.heygen.com/videos/video-1.mp4":
            return httpx.Response(
                200,
                content=b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes",
                headers={"content-type": "video/mp4", "content-length": "28"},
                request=request,
            )
        return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    task = claimed_talking_head_task(upstreamJobId="video-1")
    result = process_generation_task(
        components(adapter, queue), task, poll_interval=0
    )
    assert result["status"] == "RIGHTS_BLOCKED"
    assert _submits(calls) == []
    assert len(queue.intakes) == 1


def test_status_timeout_does_not_retry(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": {"video_id": "video-1"}}, request=request)
        return httpx.Response(
            200,
            json={"data": {"video_id": "video-1", "status": "processing", "progress": 10}},
            request=request,
        )

    adapter = HeyGenTalkingHeadAdapter(
        enabled=True, transport=httpx.MockTransport(handler)
    )
    queue = RecordingQueue()
    monkeypatch.setenv("AETHER_TALKING_HEAD_TIMEOUT_SECONDS", "2")
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    result = process_generation_task(
        components(adapter, queue), claimed_talking_head_task(), poll_interval=0
    )
    assert result["status"] == "UNKNOWN"
    assert result["error_code"] == "STATUS_TIMEOUT"
    assert len(_submits(calls)) == 1
