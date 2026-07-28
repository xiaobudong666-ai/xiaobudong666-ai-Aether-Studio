import pytest
import time
import threading
import httpx
from http.server import HTTPServer
from app.ffmpeg_adapter import FFmpegAdapter
from app.ai_provider import AIProviderInterface
from app.recovery import TaskRecoveryManager
from app.main import WorkerHealthHandler

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

def test_worker_real_http_health_check():
    # Start the real HTTPServer on a custom port inside a daemon thread for verification
    test_port = 8019
    server = HTTPServer(("127.0.0.1", test_port), WorkerHealthHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Wait for startup
    time.sleep(0.5)

    try:
        # Perform HTTP GET request using httpx
        response = httpx.get(f"http://127.0.0.1:{test_port}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "worker"
        assert "uptime_seconds" in data
    finally:
        server.shutdown()
        server.server_close()
