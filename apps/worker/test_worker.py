import threading
from unittest.mock import MagicMock
import httpx
from app.ffmpeg_adapter import FFmpegAdapter
from app.ai_provider import AIProviderInterface
from app.recovery import TaskRecoveryManager
from app.main import create_health_server, initialize_worker, process_m1_moneyprinter_task, WorkerComponents

def test_ffmpeg_adapter_mock():
    # Clearly marked as mock adapter tests
    adapter = FFmpegAdapter()
    assert adapter.target_width == 854
    assert adapter.target_height == 480
    assert adapter.create_480p_proxy("input.mp4", "output.mp4") is True
    assert adapter.extract_audio("input.mp4", "output.wav") is True

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
        moneyprinter=mock_adapter
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
        moneyprinter=mock_adapter
    )

    res = process_m1_moneyprinter_task(components, {"subject": "nature"})
    assert res["status"] == "failed"
    assert "reason" in res
    mock_adapter.generate_video.assert_not_called()
