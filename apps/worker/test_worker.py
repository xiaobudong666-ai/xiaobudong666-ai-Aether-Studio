import subprocess
import threading
from unittest.mock import MagicMock

import httpx
import pytest
from app.ai_provider import AIProviderInterface
from app.ffmpeg_adapter import FFmpegAdapter, MediaProcessingError
from app.main import (
    WorkerComponents,
    create_health_server,
    initialize_worker,
    process_m1_moneyprinter_task,
    process_render_task,
)
from app.recovery import TaskRecoveryManager


def test_ffmpeg_adapter_executes_real_proxy_audio_and_probe(tmp_path):
    source = tmp_path / "source.mp4"
    proxy = tmp_path / "proxy.mp4"
    audio = tmp_path / "audio.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
    )

    adapter = FFmpegAdapter(timeout_seconds=60)
    assert adapter.target_width == 854
    assert adapter.target_height == 480
    assert adapter.create_480p_proxy(str(source), str(proxy)) is True
    assert adapter.extract_audio(str(source), str(audio)) is True

    proxy_metadata = adapter.probe_media(proxy)
    audio_metadata = adapter.probe_media(audio)
    assert proxy_metadata["video"]["width"] == 854
    assert proxy_metadata["video"]["height"] == 480
    assert proxy_metadata["duration_seconds"] == pytest.approx(1.0, abs=0.1)
    assert audio_metadata["audio"]["sample_rate"] == 16000


def test_ffmpeg_adapter_fails_loudly_for_missing_input(tmp_path):
    adapter = FFmpegAdapter()
    with pytest.raises(MediaProcessingError, match="does not exist"):
        adapter.create_480p_proxy(
            str(tmp_path / "missing.mp4"),
            str(tmp_path / "proxy.mp4"),
        )

def test_ai_provider_mock():
    # Clearly marked as mock AI provider tests
    ai = AIProviderInterface(provider_name="TestAI")
    assert ai.provider_name == "TestAI"
    subs = ai.generate_subtitles("audio.wav")
    assert len(subs) == 2
    assert subs[0]["text"] == "Welcome to Aether Studio!"

    style_img = ai.cartoon_style_transfer("frame.png")
    assert "frame.png_stylized" in style_img

def test_recovery_manager_mock():
    # Clearly marked as mock recovery tests
    recovery = TaskRecoveryManager(backend_url="http://localhost:8000")
    recovered = recovery.scan_and_recover_tasks()
    assert recovered == []

def test_worker_reads_backend_url_from_environment(monkeypatch):
    monkeypatch.setenv("BACKEND_URL", "http://api.internal:8123")
    components = initialize_worker()
    assert components.recovery.backend_url == "http://api.internal:8123"
    assert components.queue.backend_url == "http://api.internal:8123"


def test_worker_real_http_health_check_uses_dynamic_port():
    server = create_health_server(host="127.0.0.1", port=0)
    test_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            response = client.get(f"http://127.0.0.1:{test_port}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "worker"
        assert "uptime_seconds" in data
    finally:
        server.shutdown()
        server.server_close()


def test_process_m1_moneyprinter_task_healthy():
    mock_adapter = MagicMock()
    mock_adapter.check_health.return_value = {"status": "healthy"}
    mock_adapter.generate_video.return_value = "mpt-task-555"
    mock_adapter.get_task_status.return_value = {"task_id": "mpt-task-555", "status": "processing"}

    components = WorkerComponents(
        ffmpeg=MagicMock(),
        ai=MagicMock(),
        recovery=MagicMock(),
        moneyprinter=mock_adapter,
        video_use=MagicMock(),
    )

    res = process_m1_moneyprinter_task(components, {"subject": "nature"})
    assert res["status"] == "processing"
    assert res["task_id"] == "mpt-task-555"
    mock_adapter.check_health.assert_called_once()
    mock_adapter.generate_video.assert_called_once_with(
        subject="nature", aspect="9:16", voice_name="en-US-JennyNeural"
    )
    mock_adapter.get_task_status.assert_called_once_with("mpt-task-555")


def test_process_m1_moneyprinter_task_unhealthy():
    mock_adapter = MagicMock()
    mock_adapter.check_health.return_value = {"status": "unhealthy"}

    components = WorkerComponents(
        ffmpeg=MagicMock(),
        ai=MagicMock(),
        recovery=MagicMock(),
        moneyprinter=mock_adapter,
        video_use=MagicMock(),
    )

    res = process_m1_moneyprinter_task(components, {"subject": "nature"})
    assert res["status"] == "failed"
    assert "reason" in res
    mock_adapter.generate_video.assert_not_called()


def test_worker_claimed_render_is_submitted_polled_and_persisted():
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    video_use = MagicMock()
    video_use.submit_render.return_value = {
        "jobId": "task-1", "projectId": "project-1", "status": "queued", "progress": 0, "message": "Queued"
    }
    video_use.get_job_status.side_effect = [
        {"jobId": "task-1", "projectId": "project-1", "status": "processing", "progress": 50, "message": "Rendering"},
        {"jobId": "task-1", "projectId": "project-1", "status": "completed", "progress": 100, "message": "Done", "artifact": "projects/project-1/renders/task-1/final.mp4"},
    ]
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=video_use, queue=queue,
    )
    task = {
        "taskId": "task-1", "progress": 0,
        "renderPayload": {"projectId": "project-1", "requestId": "task-1"},
        "upstreamJobId": None,
    }
    result = process_render_task(components, task, poll_interval=0)
    assert result["status"] == "completed"
    video_use.submit_render.assert_called_once_with(task["renderPayload"])
    assert queue.update.call_count == 3
    assert queue.update.call_args.kwargs["upstream_job_id"] == "task-1"


def test_worker_unknown_video_use_handoff_never_retries_without_ack():
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    video_use = MagicMock()
    video_use.submit_render.side_effect = RuntimeError("temporary network failure")
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=video_use, queue=queue,
    )
    result = process_render_task(
        components,
        {"taskId": "task-retry", "progress": 0,
         "renderPayload": {"projectId": "p", "requestId": "task-retry"}},
        poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] is None
    video_use.submit_render.assert_called_once()


def test_worker_unknown_poll_keeps_durable_upstream_id_for_manual_reconcile():
    """FAKE-only: no second Sidecar submit after status polling times out."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    video_use = MagicMock()
    video_use.get_job_status.side_effect = TimeoutError("FAKE lost polling ACK")
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=video_use, queue=queue,
    )
    result = process_render_task(
        components,
        {"taskId": "fake-poll-lost-ack", "progress": 50,
         "renderPayload": {"projectId": "synthetic", "requestId": "fake-poll-lost-ack"},
         "upstreamJobId": "fake-poll-lost-ack"},
        poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] == "fake-poll-lost-ack"
    video_use.submit_render.assert_not_called()
    video_use.get_job_status.assert_called_once_with("fake-poll-lost-ack")



@pytest.mark.parametrize(
    "sidecar_ack",
    [
        {"jobId": "other-task", "projectId": "project-1"},
        {"jobId": "task-bound", "projectId": "project-other"},
        {"jobId": None, "projectId": "project-1"},
        {"jobId": "task-bound"},
    ],
)
def test_render_sidecar_ack_must_match_immutable_task_and_project(sidecar_ack):
    """FAKE-only negative receipt: no unrelated job is ever polled or bound."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    sidecar.submit_render.return_value = sidecar_ack
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    payload = {"projectId": "project-1", "requestId": "task-bound"}
    result = process_render_task(
        components, {"taskId": "task-bound", "progress": 0,
                     "renderPayload": payload}, poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] is None
    sidecar.submit_render.assert_called_once_with(payload)
    sidecar.get_job_status.assert_not_called()
    queue.update.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"projectId": "project-1"},
        {"projectId": "project-1", "requestId": "wrong-task"},
        {"projectId": "", "requestId": "task-bound"},
        {"requestId": "task-bound"},
    ],
)
def test_render_unbound_request_never_reaches_sidecar(payload):
    """FAKE-only: reject bad request identity before any mutating POST."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    result = process_render_task(
        components, {"taskId": "task-bound", "progress": 0,
                     "renderPayload": payload}, poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    sidecar.submit_render.assert_not_called()
    sidecar.get_job_status.assert_not_called()
    queue.update.assert_called_once()



@pytest.mark.parametrize("upstream_job_id", ["unrelated-sidecar-job", 123])
def test_render_resume_rejects_mismatched_persisted_sidecar_identity(upstream_job_id):
    """FAKE-only: a previous bad ACK must never authorize polling another job."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    result = process_render_task(
        components, {
            "taskId": "task-bound", "progress": 0,
            "renderPayload": {"projectId": "project-1", "requestId": "task-bound"},
            "upstreamJobId": upstream_job_id,
        }, poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    sidecar.get_job_status.assert_not_called()
    sidecar.submit_render.assert_not_called()



@pytest.mark.parametrize(
    "sidecar_status",
    [
        {"jobId": "unrelated-job", "projectId": "project-1", "status": "completed"},
        {"jobId": "task-bound", "projectId": "different-project", "status": "completed"},
        {"projectId": "project-1", "status": "completed"},
        {"jobId": "task-bound", "status": "completed"},
        {"jobId": "unrelated-job", "projectId": "project-1", "status": "processing"},
        [],
    ],
)
@pytest.mark.parametrize("resume", [False, True])
def test_render_sidecar_poll_identity_must_match_job_and_project(sidecar_status, resume):
    """FAKE-only: a forged/missing GET status never authorizes a terminal write."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    sidecar.submit_render.return_value = {
        "jobId": "task-bound", "projectId": "project-1", "status": "queued"
    }
    sidecar.get_job_status.return_value = sidecar_status
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    task = {
        "taskId": "task-bound", "progress": 15,
        "renderPayload": {"projectId": "project-1", "requestId": "task-bound"},
    }
    if resume:
        task["upstreamJobId"] = "task-bound"
    result = process_render_task(components, task, poll_interval=0)
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] == "task-bound"
    sidecar.get_job_status.assert_called_once_with("task-bound")
    if resume:
        sidecar.submit_render.assert_not_called()
    else:
        sidecar.submit_render.assert_called_once()
    assert not any(
        call.kwargs.get("status") == "completed"
        for call in queue.update.call_args_list
    )




@pytest.mark.parametrize(
    "artifact",
    [
        None,
        "",
        "/absolute/path/final.mp4",
        "projects/project-other/renders/task-bound/final.mp4",
        "projects/project-1/renders/another-task/final.mp4",
        "projects/project-1/renders/task-bound/../../another-task/final.mp4",
        "projects/project-1/renders/task-bound/preview.mp4",
        {"path": "projects/project-1/renders/task-bound/final.mp4"},
    ],
)
@pytest.mark.parametrize("resume", [False, True])
def test_render_completed_sidecar_status_requires_bound_final_artifact(artifact, resume):
    """FAKE-only: a status-only completed claim must not create a READY candidate."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    sidecar.submit_render.return_value = {
        "jobId": "task-bound", "projectId": "project-1", "status": "queued",
    }
    sidecar.get_job_status.return_value = {
        "jobId": "task-bound", "projectId": "project-1",
        "status": "completed", "progress": 100,
        "artifact": artifact,
    }
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    task = {
        "taskId": "task-bound", "progress": 15,
        "renderPayload": {"projectId": "project-1", "requestId": "task-bound"},
    }
    if resume:
        task["upstreamJobId"] = "task-bound"
    result = process_render_task(components, task, poll_interval=0)
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] == "task-bound"
    assert not any(
        call.kwargs.get("status") == "completed"
        for call in queue.update.call_args_list
    )
    sidecar.get_job_status.assert_called_once_with("task-bound")
    if resume:
        sidecar.submit_render.assert_not_called()
    else:
        sidecar.submit_render.assert_called_once()


@pytest.mark.parametrize("resume", [False, True])
def test_render_completed_sidecar_status_accepts_only_exact_bound_final_path(resume):
    """FAKE-only positive contract: normal completed path remains usable."""
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = MagicMock()
    sidecar.submit_render.return_value = {
        "jobId": "task-bound", "projectId": "project-1", "status": "queued",
    }
    sidecar.get_job_status.return_value = {
        "jobId": "task-bound", "projectId": "project-1",
        "status": "completed", "progress": 100,
        "artifact": "projects/project-1/renders/task-bound/final.mp4",
    }
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    task = {
        "taskId": "task-bound", "progress": 0,
        "renderPayload": {"projectId": "project-1", "requestId": "task-bound"},
    }
    if resume:
        task["upstreamJobId"] = "task-bound"
    result = process_render_task(components, task, poll_interval=0)
    assert result["status"] == "completed"
    assert result["upstream_job_id"] == "task-bound"
    if resume:
        sidecar.submit_render.assert_not_called()
    else:
        sidecar.submit_render.assert_called_once()


from app.video_use_adapter import VideoUseAdapter, VideoUseError


@pytest.mark.parametrize("failure", ["lost_ack_timeout", "http_503"])
def test_g70_g71_worker_sidecar_post_ambiguous_ack_never_replays(monkeypatch, failure):
    """FAKE-only: POST may have been committed before a lost ACK or HTTP 503."""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            request = httpx.Request(method, url)
            if failure == "lost_ack_timeout":
                raise httpx.ReadTimeout("FAKE render accepted; ACK lost", request=request)
            return httpx.Response(
                503, json={"detail": "FAKE ambiguous upstream"}, request=request
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    adapter = VideoUseAdapter(
        api_url="http://fake-sidecar", max_retries=3, backoff_factor=0
    )
    with pytest.raises(VideoUseError):
        adapter.submit_render({"projectId": "p", "requestId": "id"})
    assert calls == [("POST", "http://fake-sidecar/renders")]


def test_g70_g71_worker_sidecar_read_only_status_get_can_retry(monkeypatch):
    """Read-only polling remains retriable; this must not relax the POST fence."""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            request = httpx.Request(method, url)
            if len(calls) == 1:
                raise httpx.ReadTimeout("FAKE status read timeout", request=request)
            return httpx.Response(
                200,
                json={"jobId": "fake-job", "status": "processing"},
                request=request,
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    adapter = VideoUseAdapter(
        api_url="http://fake-sidecar", max_retries=2, backoff_factor=0
    )
    assert adapter.get_job_status("fake-job")["status"] == "processing"
    assert calls == [("GET", "http://fake-sidecar/jobs/fake-job")] * 2



def test_g70_g71_sidecar_corrupt_completed_bytes_409_fails_closed_without_resubmission(monkeypatch):
    """FAKE-only transport regression for Sidecar integrity HTTP 409 boundary."""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            request = httpx.Request(method, url)
            return httpx.Response(
                409,
                json={"detail": "Completed render byte proof unavailable; manual reconciliation required"},
                request=request,
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    queue = MagicMock()
    queue.update.side_effect = lambda _task_id, **values: values
    sidecar = VideoUseAdapter(api_url="http://fake-sidecar", max_retries=1, backoff_factor=0)
    components = WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=MagicMock(), video_use=sidecar, queue=queue,
    )
    result = process_render_task(
        components,
        {
            "taskId": "byte-proof-job",
            "progress": 50,
            "upstreamJobId": "byte-proof-job",
            "renderPayload": {"projectId": "byte-proof-project", "requestId": "byte-proof-job"},
        },
        poll_interval=0,
    )
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    assert result["upstream_job_id"] == "byte-proof-job"
    assert all(method == "GET" for method, _url in calls)
    assert calls == [("GET", "http://fake-sidecar/jobs/byte-proof-job")] * 2
    queue.update.assert_called_once()
    assert queue.update.call_args.kwargs["status"] == "UNKNOWN"
