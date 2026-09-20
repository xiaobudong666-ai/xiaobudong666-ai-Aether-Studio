"""W1: deterministic-fake Worker adapter contract and golden-chain topology.

All tests are strictly local and fake-only: no Provider network, no credentials,
no billing. They prove that fake-only Generation passes through the same Worker
claim/transition/artifact-intake topology with 1 task / 1 attempt / 0 retry /
0 fallback, and produces a real 1080x1920 MP4.
"""
import inspect
import json
import subprocess

import pytest
from app.deterministic_fake_adapter import (
    ADAPTER_VERSION,
    DeterministicFakeAdapter,
    DeterministicFakeError,
)
from app.main import (
    DisabledMoneyPrinterAdapter,
    WorkerComponents,
    attest_worker_provider,
    initialize_worker,
    operator_generation_mode,
    process_generation_task,
)
from app.moneyprinter_adapter import MoneyPrinterTurboAdapter


def probe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def write_tmp(tmp_path, data, name="artifact.mp4"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def claimed_task(**overrides):
    task = {
        "taskId": "generation-w1",
        "projectId": "project-w1",
        "attempt": 1,
        "providerMode": "deterministic-fake",
        "upstreamJobId": None,
        "request": {
            "videoSubject": "deterministic fake prompt",
            "videoAspect": "9:16",
            "voiceName": "en-US-JennyNeural",
            "videoConcatMode": "random",
            "videoClipDuration": 5,
        },
    }
    task.update(overrides)
    return task


class RecordingQueue:
    def __init__(self):
        self.transitions = []
        self.heartbeats = 0
        self.intakes = []

    def claim(self):
        return None

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


def components(provider, queue):
    return WorkerComponents(
        ffmpeg=None, ai=None, recovery=None,
        moneyprinter=provider, video_use=None, generation_queue=queue,
    )


# ---------------------------------------------------------------- adapter unit
def test_adapter_generate_is_deterministic_and_local():
    adapter = DeterministicFakeAdapter()
    first = adapter.generate_video("hello", aspect="9:16", video_clip_duration=3)
    second = adapter.generate_video("hello", aspect="9:16", video_clip_duration=3)
    assert first == second
    assert first.startswith("fake-")
    assert len(first) == 5 + 40

    status = adapter.get_task_status(first)
    assert status["status"] == "completed"
    assert status["progress"] == 100
    assert status["providerArtifactId"] == first

    payload = adapter.stream_artifact(first).read()
    assert payload.startswith(b"\x00\x00\x00")
    assert len(payload) > 1000


def test_adapter_stream_is_1080x1920_with_video_and_audio(tmp_path):
    adapter = DeterministicFakeAdapter()
    job = adapter.generate_video("vertical", aspect="9:16", video_clip_duration=3)
    payload = adapter.stream_artifact(job).read()
    path = write_tmp(tmp_path, payload)

    info = probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert video["width"] == 1080
    assert video["height"] == 1920
    assert video["codec_name"] in {"h264", "hevc"}
    assert audio["codec_name"] == "aac"
    duration = float(info["format"]["duration"])
    assert 2.5 <= duration <= 3.5


def test_adapter_rejects_unsafe_inputs():
    adapter = DeterministicFakeAdapter()
    with pytest.raises(DeterministicFakeError):
        adapter.generate_video("", aspect="9:16")
    with pytest.raises(DeterministicFakeError):
        adapter.generate_video("x", aspect="4:3")
    with pytest.raises(DeterministicFakeError):
        adapter.generate_video("x", video_clip_duration=11)
    with pytest.raises(DeterministicFakeError):
        adapter.get_task_status("../escape")


def test_adapter_has_no_network_or_credential_surface():
    source = inspect.getsource(DeterministicFakeAdapter)
    module = inspect.getsource(__import__("app.deterministic_fake_adapter", fromlist=["x"]))
    for forbidden in ("httpx", "requests", "urllib.request", "socket.", "os.environ.get"):
        assert forbidden not in module, forbidden
    assert "CREDENTIAL" not in source
    assert "AETHER_GENERATION_CREDENTIAL_STATE" not in source


# ------------------------------------------------------------- mode selection
def test_operator_generation_mode_defaults_disabled(monkeypatch):
    monkeypatch.delenv("AETHER_GENERATION_PROVIDER_MODE", raising=False)
    assert operator_generation_mode() == "disabled"


def test_operator_generation_mode_accepts_deterministic_fake(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "deterministic-fake")
    assert operator_generation_mode() == "deterministic-fake"


def test_operator_generation_mode_rejects_unknown(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "some-unknown-provider")
    assert operator_generation_mode() == "disabled"


def test_initialize_worker_selects_deterministic_fake(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "deterministic-fake")
    comps = initialize_worker()
    assert isinstance(comps.moneyprinter, DeterministicFakeAdapter)


def test_initialize_worker_keeps_moneyprinter_and_default_unchanged(monkeypatch):
    monkeypatch.delenv("AETHER_GENERATION_PROVIDER_MODE", raising=False)
    assert isinstance(initialize_worker().moneyprinter, DisabledMoneyPrinterAdapter)

    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "moneyprinter")
    monkeypatch.delenv("AETHER_GENERATION_CREDENTIAL_STATE", raising=False)
    assert isinstance(initialize_worker().moneyprinter, DisabledMoneyPrinterAdapter)


# ----------------------------------------------------------- worker topology
def test_process_generation_task_fake_golden_chain_single_attempt(tmp_path):
    adapter = DeterministicFakeAdapter()
    queue = RecordingQueue()

    result = process_generation_task(components(adapter, queue), claimed_task(), poll_interval=0)

    assert result["status"] == "RIGHTS_BLOCKED"
    statuses = [t[1]["status"] for t in queue.transitions]
    assert statuses == ["RUNNING", "INGESTING"]
    # 0 retry / 0 fallback: no transition may request a retry.
    assert all(not t[1].get("retryable") for t in queue.transitions)
    assert queue.heartbeats == 1
    assert len(queue.intakes) == 1

    task_id, provider_artifact_id, payload = queue.intakes[0]
    assert task_id == "generation-w1"
    assert provider_artifact_id.startswith("fake-")
    path = write_tmp(tmp_path, payload)
    info = probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert video["width"] == 1080 and video["height"] == 1920
    assert any(s["codec_type"] == "audio" for s in info["streams"])


def test_attest_worker_provider_skips_deterministic_fake(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "deterministic-fake")
    queue = RecordingQueue()
    comps = components(DeterministicFakeAdapter(), queue)
    attest_worker_provider(comps)
    assert not hasattr(queue, "attested")


def test_worker_provider_adapter_version_is_pinned():
    assert ADAPTER_VERSION == "aether-deterministic-fake-v1"
