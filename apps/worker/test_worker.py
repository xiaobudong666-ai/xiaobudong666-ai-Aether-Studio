import pytest
from app.ffmpeg_adapter import FFmpegAdapter
from app.ai_provider import AIProviderInterface
from app.recovery import TaskRecoveryManager

def test_ffmpeg_adapter():
    adapter = FFmpegAdapter()
    assert adapter.target_width == 854
    assert adapter.target_height == 480
    assert adapter.create_480p_proxy("input.mp4", "output.mp4") is True
    assert adapter.extract_audio("input.mp4", "output.wav") is True

def test_ai_provider():
    ai = AIProviderInterface(provider_name="TestAI")
    assert ai.provider_name == "TestAI"
    subs = ai.generate_subtitles("audio.wav")
    assert len(subs) == 2
    assert subs[0]["text"] == "Welcome to Aether Studio!"

    style_img = ai.cartoon_style_transfer("frame.png")
    assert "frame.png_stylized" in style_img

def test_recovery_manager():
    recovery = TaskRecoveryManager(backend_url="http://localhost:8000")
    recovered = recovery.scan_and_recover_tasks()
    assert recovered == []
