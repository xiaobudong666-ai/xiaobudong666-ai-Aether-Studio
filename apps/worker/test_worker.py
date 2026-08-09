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
        "jobId": "upstream-1", "status": "queued", "progress": 0, "message": "Queued"
    }
    video_use.get_job_status.side_effect = [
        {"status": "processing", "progress": 50, "message": "Rendering"},
        {"status": "completed", "progress": 100, "message": "Done"},
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
    assert queue.update.call_args.kwargs["upstream_job_id"] == "upstream-1"


def test_worker_requeues_transient_video_use_failure():
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
        {"taskId": "task-retry", "progress": 0, "renderPayload": {"projectId": "p"}},
        poll_interval=0,
    )
    assert result["status"] == "failed"
    assert result["retryable"] is True
