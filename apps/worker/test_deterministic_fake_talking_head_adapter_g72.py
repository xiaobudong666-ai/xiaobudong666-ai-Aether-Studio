"""G72 FAKE-only talking-head adapter contract; no paid provider or HTTP."""
from __future__ import annotations

import io
import pytest

from app.deterministic_fake_adapter import DeterministicFakeAdapter, DeterministicFakeError
from app.deterministic_fake_talking_head_adapter import DeterministicFakeTalkingHeadAdapter
from app.talking_head_provider_adapter import TalkingHeadJobSpec, TalkingHeadProviderError


def make_spec(**overrides):
    params = {
        "text": "offline synthetic fixture",
        "duration_seconds": 1,
        "aspect_ratio": "9:16",
        "resolution": "720p",
        "voice_id": "synthetic-seed",
        "image_asset_id": "asset-version-1",
    }
    params.update(overrides)
    return TalkingHeadJobSpec(**params)


def assert_error(expected_code, action):
    with pytest.raises(TalkingHeadProviderError) as caught:
        action()
    assert caught.value.code == expected_code
    assert caught.value.retryable is False


def test_fake_mode_declares_no_network():
    adapter = DeterministicFakeTalkingHeadAdapter(DeterministicFakeAdapter())
    assert adapter.network == "none"
    assert adapter.provider == "deterministic-fake-talking-head"


def test_non_fake_generator_rejected():
    with pytest.raises(TypeError):
        DeterministicFakeTalkingHeadAdapter(object())


def test_remote_visual_or_audio_source_rejected():
    adapter = DeterministicFakeTalkingHeadAdapter(DeterministicFakeAdapter())
    assert_error("FAKE_VISUAL_OR_AUDIO_SCOPE_INVALID", lambda: adapter.submit(make_spec(
        image_asset_id=None, image_url="https://assets.invalid/a.jpg"
    )))
    assert_error("JOB_SPEC_INVALID", lambda: adapter.submit(make_spec(avatar_id="external-avatar")))
    assert_error("FAKE_VISUAL_OR_AUDIO_SCOPE_INVALID", lambda: adapter.submit(make_spec(
        audio_asset_id="external-audio", voice_id=None
    )))
    assert_error("FAKE_VISUAL_OR_AUDIO_SCOPE_INVALID", lambda: adapter.submit(make_spec(
        audio_url="https://assets.invalid/a.wav", voice_id=None
    )))


def test_missing_visual_blocked_by_provider_contract():
    adapter = DeterministicFakeTalkingHeadAdapter(DeterministicFakeAdapter())
    assert_error("JOB_SPEC_INVALID", lambda: adapter.submit(make_spec(image_asset_id=None)))


def test_missing_voice_blocked_by_provider_contract():
    adapter = DeterministicFakeTalkingHeadAdapter(DeterministicFakeAdapter())
    assert_error("JOB_SPEC_INVALID", lambda: adapter.submit(make_spec(voice_id=None)))


def test_submit_status_artifact_are_offline_translations(monkeypatch):
    native = DeterministicFakeAdapter()
    calls = []

    def generate_video(**kwargs):
        calls.append(kwargs)
        return "fake-native-id"

    monkeypatch.setattr(native, "generate_video", generate_video)
    monkeypatch.setattr(native, "get_task_status", lambda job_id: {
        "status": "completed", "jobId": job_id
    })
    monkeypatch.setattr(native, "stream_artifact", lambda job_id: io.BytesIO(b"synthetic-video"))
    adapter = DeterministicFakeTalkingHeadAdapter(native)

    job_id = adapter.submit(make_spec())
    assert job_id == "fake-native-id"
    assert calls == [{
        "subject": "offline synthetic fixture",
        "aspect": "9:16",
        "voice_name": "synthetic-seed",
        "video_concat_mode": "sequential",
        "video_clip_duration": 1,
    }]
    assert adapter.status(job_id)["status"] == "completed"
    assert adapter.artifact(job_id).read() == b"synthetic-video"


def test_generator_error_sanitized_nonretryable(monkeypatch):
    native = DeterministicFakeAdapter()

    def fail(**kwargs):
        raise DeterministicFakeError("unsafe internal detail")

    monkeypatch.setattr(native, "generate_video", fail)
    adapter = DeterministicFakeTalkingHeadAdapter(native)
    assert_error("FAKE_SUBMIT_FAILED", lambda: adapter.submit(make_spec()))


def test_missing_status_and_artifact_sanitized_nonretryable(monkeypatch):
    native = DeterministicFakeAdapter()

    def fail(_job_id):
        raise DeterministicFakeError("unsafe internal detail")

    monkeypatch.setattr(native, "get_task_status", fail)
    monkeypatch.setattr(native, "stream_artifact", fail)
    adapter = DeterministicFakeTalkingHeadAdapter(native)
    assert_error("FAKE_STATUS_UNKNOWN", lambda: adapter.status("missing-fake-id"))
    assert_error("FAKE_ARTIFACT_UNAVAILABLE", lambda: adapter.artifact("missing-fake-id"))


def test_g72_native_ffmpeg_media_contains_h264_and_aac(tmp_path, monkeypatch):
    """Exercise real OFFLINE encoding in the FFmpeg Worker image, without media mocks."""
    import json
    import shutil
    import subprocess
    from app import deterministic_fake_adapter as fake_module

    assert shutil.which("ffmpeg"), "FFmpeg is mandatory for this Worker acceptance test"
    assert shutil.which("ffprobe"), "ffprobe is mandatory for real media verification"
    monkeypatch.setitem(fake_module._ASPECT_DIMENSIONS, "9:16", (160, 288))
    fake = DeterministicFakeAdapter(timeout_seconds=90)
    adapter = DeterministicFakeTalkingHeadAdapter(fake)
    assert adapter.network == "none"
    job_id = adapter.submit(make_spec(text="G72 actual local FFmpeg media check"))
    assert adapter.status(job_id)["status"] == "completed"
    video_bytes = adapter.artifact(job_id).read()
    assert len(video_bytes) > 256 and b"ftyp" in video_bytes[:64]
    artifact = tmp_path / "g72-synthetic-local.mp4"
    artifact.write_bytes(video_bytes)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height:format=duration",
         "-of", "json", str(artifact)],
        capture_output=True, text=True, check=True, timeout=15,
    )
    probe = json.loads(result.stdout)
    video_streams = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audio_streams = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(video_streams) == len(audio_streams) == 1
    assert video_streams[0]["codec_name"] == "h264"
    assert (video_streams[0]["width"], video_streams[0]["height"]) == (160, 288)
    assert audio_streams[0]["codec_name"] == "aac"
    assert 0.8 <= float(probe["format"]["duration"]) <= 1.3
