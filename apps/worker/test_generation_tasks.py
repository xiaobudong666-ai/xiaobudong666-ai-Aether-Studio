import inspect
import io
from unittest.mock import MagicMock

import pytest
from app.generation_queue import GenerationQueueClient, GenerationQueueError
from app.main import WorkerComponents, process_generation_task


def components(provider, queue):
    return WorkerComponents(
        ffmpeg=MagicMock(), ai=MagicMock(), recovery=MagicMock(),
        moneyprinter=provider, video_use=MagicMock(), generation_queue=queue,
    )


def claimed_task(**overrides):
    task = {
        "taskId": "generation-1",
        "projectId": "project-1",
        "attempt": 1,
        "providerMode": "deterministic-fake",
        "upstreamJobId": None,
        "request": {
            "videoSubject": "deterministic prompt",
            "videoAspect": "9:16",
            "voiceName": "en-US-JennyNeural",
            "videoConcatMode": "random",
            "videoClipDuration": 5,
        },
    }
    task.update(overrides)
    return task


def queue_mock():
    queue = MagicMock()
    queue.transition.side_effect = lambda task_id, **values: {"taskId": task_id, **values}
    queue.heartbeat.return_value = {"leaseExpiresAt": "later"}
    queue.artifact_intake.return_value = {"taskId": "generation-1", "status": "RIGHTS_BLOCKED"}
    return queue


def test_runtime_disabled_never_calls_provider():
    provider = MagicMock()
    queue = queue_mock()
    result = process_generation_task(components(provider, queue), claimed_task(providerMode="disabled"), poll_interval=0)
    assert result["error_code"] == "PROVIDER_DISABLED"
    provider.generate_video.assert_not_called()


def test_deterministic_fake_submits_polls_and_streams_bytes():
    provider = MagicMock()
    provider.generate_video.return_value = "upstream-1"
    provider.get_task_status.return_value = {
        "status": "completed", "progress": 100, "providerArtifactId": "artifact-1",
    }
    provider.stream_artifact.return_value = io.BytesIO(b"deterministic-video")
    queue = queue_mock()
    result = process_generation_task(components(provider, queue), claimed_task(), poll_interval=0)
    assert result["status"] == "RIGHTS_BLOCKED"
    provider.generate_video.assert_called_once()
    provider.stream_artifact.assert_called_once_with("artifact-1")
    queue.artifact_intake.assert_called_once()


def test_restart_with_upstream_id_queries_without_reposting():
    provider = MagicMock()
    provider.get_task_status.return_value = {
        "status": "completed", "providerArtifactId": "artifact-recovered",
    }
    provider.stream_artifact.return_value = b"video"
    queue = queue_mock()
    process_generation_task(
        components(provider, queue), claimed_task(upstreamJobId="upstream-existing"), poll_interval=0,
    )
    provider.generate_video.assert_not_called()
    provider.get_task_status.assert_called_once_with("upstream-existing")


def test_ambiguous_submission_becomes_unknown_without_repost():
    provider = MagicMock()
    provider.generate_video.side_effect = TimeoutError("response lost")
    queue = queue_mock()
    result = process_generation_task(components(provider, queue), claimed_task(), poll_interval=0)
    assert result["status"] == "UNKNOWN"
    assert result["retryable"] is False
    provider.generate_video.assert_called_once()


def test_provider_4xx_is_non_retryable():
    class ProviderRejected(RuntimeError):
        status_code = 400

    provider = MagicMock()
    provider.generate_video.side_effect = ProviderRejected("invalid input")
    queue = queue_mock()
    result = process_generation_task(components(provider, queue), claimed_task(), poll_interval=0)
    assert result["error_code"] == "PROVIDER_4XX"
    assert result["retryable"] is False


def test_provider_artifact_url_is_ignored_without_trusted_identifier():
    provider = MagicMock()
    provider.generate_video.return_value = "upstream-1"
    provider.get_task_status.return_value = {
        "status": "completed", "video_url": "https://evil.example/video.mp4",
    }
    queue = queue_mock()
    result = process_generation_task(components(provider, queue), claimed_task(), poll_interval=0)
    assert result["error_code"] == "ARTIFACT_ID_MISSING"
    provider.stream_artifact.assert_not_called()


def test_worker_generation_modules_never_import_database_models():
    import app.generation_queue as generation_queue_module
    import app.main as worker_main_module

    source = inspect.getsource(generation_queue_module) + inspect.getsource(worker_main_module)
    assert "from .models" not in source
    assert "sqlalchemy" not in source


def test_generation_queue_requires_worker_token():
    client = GenerationQueueClient(worker_token="")
    with pytest.raises(GenerationQueueError, match="not configured"):
        client.claim()
