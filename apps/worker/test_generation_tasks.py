import inspect
import io
import datetime
from unittest.mock import MagicMock

import httpx
import pytest
from app.generation_queue import GenerationQueueClient, GenerationQueueError
from app.main import (
    DisabledMoneyPrinterAdapter,
    WorkerComponents,
    attest_worker_provider,
    initialize_worker,
    process_generation_task,
)
from app.moneyprinter_adapter import ADAPTER_VERSION, UPSTREAM_PIN, MoneyPrinterTurboAdapter


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


def test_im16_31_deterministic_fake_stream_is_ingested_once_with_artifact_id():
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


def test_im16_23_restart_with_upstream_id_queries_without_reposting():
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


def test_im16_19_moneyprinter_mode_requires_matching_claim_proof(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "moneyprinter")
    monkeypatch.setenv("AETHER_GENERATION_CONFIG_VERSION_ID", "config-1")
    monkeypatch.setenv("AETHER_GENERATION_POLICY_HASH", "a" * 64)
    monkeypatch.setenv("AETHER_GENERATION_CREDENTIAL_STATE", "PRESENT")
    monkeypatch.setenv("AETHER_GENERATION_NETWORK_ISOLATION", "ENFORCED")
    monkeypatch.setenv("AETHER_GENERATION_CANARY_PROFILE", "private-one-task-v1")
    provider = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    provider.generate_video = MagicMock(return_value="upstream-1")
    provider.get_task_status = MagicMock(return_value={
        "status": "completed", "progress": 100, "providerArtifactId": "artifact-1",
    })
    provider.stream_artifact = MagicMock(return_value=io.BytesIO(b"video"))
    queue = queue_mock()
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2)
    task = claimed_task(
        providerMode="moneyprinter",
        configVersionId="config-1",
        policyHash="a" * 64,
        providerPolicy={
            "enabledIntent": True,
            "concurrentTaskLimit": 1,
            "monthlyRequestLimit": 1,
            "monthlyGeneratedSecondsLimit": 10,
            "maxClipDurationSeconds": 10,
            "maxOutputs": 1,
            "artifactPathPrefixes": ["/tasks/"],
            "maxArtifactBytes": 1024,
        },
        workerProof={
            "expiresAt": expires.isoformat().replace("+00:00", "Z"),
            "adapterVersion": ADAPTER_VERSION,
            "upstreamPin": UPSTREAM_PIN,
            "credentialState": "PRESENT",
            "networkIsolation": "ENFORCED",
            "canaryProfile": "private-one-task-v1",
        },
    )
    assert process_generation_task(components(provider, queue), task, poll_interval=0)["status"] == "RIGHTS_BLOCKED"
    provider.generate_video.assert_called_once()


def test_im18_10_moneyprinter_mode_without_sanitized_preflight_proof_is_non_network(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "moneyprinter")
    monkeypatch.setenv("AETHER_GENERATION_CREDENTIAL_STATE", "ABSENT")
    monkeypatch.setenv("AETHER_GENERATION_NETWORK_ISOLATION", "NOT_ENFORCED")
    monkeypatch.setenv("AETHER_GENERATION_CANARY_PROFILE", "disabled")
    initialized = initialize_worker()
    assert isinstance(initialized.moneyprinter, DisabledMoneyPrinterAdapter)


def test_im19_25_invalid_runtime_proof_attests_unhealthy_without_sidecar_call(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "moneyprinter")
    monkeypatch.setenv("AETHER_GENERATION_CREDENTIAL_STATE", "INVALID")
    monkeypatch.setenv("AETHER_GENERATION_NETWORK_ISOLATION", "ENFORCED")
    monkeypatch.setenv("AETHER_GENERATION_CANARY_PROFILE", "private-one-task-v1")
    provider = MagicMock()
    queue = queue_mock()
    current = components(provider, queue)
    attest_worker_provider(current)
    provider.get_capabilities.assert_not_called()
    payload = queue.attest.call_args.args[0]
    assert payload["healthy"] is False
    assert payload["reasonCode"] == "CREDENTIAL_STATE_INVALID"
    assert payload["credentialState"] == "INVALID"
    assert payload["networkIsolation"] == "ENFORCED"
    assert payload["canaryProfile"] == "private-one-task-v1"


def test_im20_30_broad_runtime_policy_fails_before_provider_post(monkeypatch):
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "moneyprinter")
    monkeypatch.setenv("AETHER_GENERATION_CONFIG_VERSION_ID", "config-1")
    monkeypatch.setenv("AETHER_GENERATION_POLICY_HASH", "a" * 64)
    monkeypatch.setenv("AETHER_GENERATION_CREDENTIAL_STATE", "PRESENT")
    monkeypatch.setenv("AETHER_GENERATION_NETWORK_ISOLATION", "ENFORCED")
    monkeypatch.setenv("AETHER_GENERATION_CANARY_PROFILE", "private-one-task-v1")
    provider = MoneyPrinterTurboAdapter(api_url="http://moneyprinter-sidecar:8080")
    provider.generate_video = MagicMock()
    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=2)
    task = claimed_task(
        providerMode="moneyprinter",
        configVersionId="config-1",
        policyHash="a" * 64,
        providerPolicy={
            "enabledIntent": True,
            "concurrentTaskLimit": 1,
            "monthlyRequestLimit": 2,
            "monthlyGeneratedSecondsLimit": 10,
            "maxClipDurationSeconds": 10,
            "maxOutputs": 1,
            "artifactPathPrefixes": ["/tasks/"],
            "maxArtifactBytes": 1024,
        },
        workerProof={
            "expiresAt": expires.isoformat().replace("+00:00", "Z"),
            "adapterVersion": ADAPTER_VERSION,
            "upstreamPin": UPSTREAM_PIN,
            "credentialState": "PRESENT",
            "networkIsolation": "ENFORCED",
            "canaryProfile": "private-one-task-v1",
        },
    )
    result = process_generation_task(components(provider, queue_mock()), task, poll_interval=0)
    assert result["error_code"] == "WORKER_PROOF_MISMATCH"
    provider.generate_video.assert_not_called()


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


def test_generation_queue_preserves_governance_rejection_code(monkeypatch):
    response = httpx.Response(
        409,
        json={"detail": {"code": "TASK_CANCELED", "message": "canceled"}},
        request=httpx.Request("POST", "http://api/internal/generation-tasks/task/heartbeat"),
    )
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.request.return_value = response
    monkeypatch.setattr("app.generation_queue.httpx.Client", lambda **_kwargs: client)

    with pytest.raises(GenerationQueueError) as caught:
        GenerationQueueClient(
            backend_url="http://api", worker_token="token", worker_id="worker",
        ).heartbeat("task")

    assert caught.value.status_code == 409
    assert caught.value.code == "TASK_CANCELED"


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("TASK_CANCELED", "CANCELED"),
        ("LEASE_LOST", "UNKNOWN"),
        ("PROVIDER_EMERGENCY_STOPPED", "UNKNOWN"),
    ],
)
def test_worker_stops_without_overwriting_queue_governance(code, expected_status):
    provider = MagicMock()
    queue = queue_mock()
    queue.heartbeat.side_effect = GenerationQueueError(
        "governed rejection", status_code=409, code=code,
    )

    result = process_generation_task(
        components(provider, queue),
        claimed_task(upstreamJobId="upstream-existing"),
        poll_interval=0,
    )

    assert result == {
        "taskId": "generation-1",
        "status": expected_status,
        "errorCode": code,
    }
    queue.transition.assert_not_called()
    provider.get_task_status.assert_not_called()
