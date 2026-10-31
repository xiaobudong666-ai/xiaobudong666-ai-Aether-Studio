import datetime
import hashlib
import inspect
import os
import threading
import json
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.auth import hash_password
from app.database import Base, build_engine
from app.main import create_app
from app.video_use_adapter import VideoUseError, VideoUseNotFoundError
from app.models import (
    DBAdoption,
    DBAssetVersion,
    DBCandidate,
    DBGenerationAttempt,
    DBGenerationCircuitState,
    DBGenerationEvent,
    DBGenerationProviderAttestation,
    DBGenerationProviderConfigVersion,
    DBGenerationProviderEvent,
    DBGenerationTask,
    DBRightsSnapshot,
    DBGenerationUsageEntry,
    DBProject,
    DBTenant,
    DBUser,
)
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import sessionmaker

OWNER_PASSWORD = secrets.token_urlsafe(24)
WORKER_TOKEN = secrets.token_urlsafe(32)

class LeaseAwareTestClient(TestClient):
    """Test helper that mirrors the Worker lease-epoch header contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lease_epochs = {}

    def post(self, url, *args, **kwargs):
        path = str(url).split("?", 1)[0]
        if path.endswith("/internal/generation-tasks/claim"):
            response = super().post(url, *args, **kwargs)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and "taskId" in payload and "leaseEpoch" in payload:
                    self._lease_epochs[str(payload["taskId"])] = int(payload["leaseEpoch"])
            return response

        prefix = "/internal/generation-tasks/"
        if path.startswith(prefix):
            parts = path[len(prefix):].split("/")
            if len(parts) >= 2 and parts[1] in {"heartbeat", "transition", "artifact-intake"}:
                task_id = parts[0]
                headers = dict(kwargs.get("headers") or {})
                if "X-Worker-Lease-Epoch" not in headers and task_id in self._lease_epochs:
                    headers["X-Worker-Lease-Epoch"] = str(self._lease_epochs[task_id])
                kwargs["headers"] = headers
        return super().post(url, *args, **kwargs)



class FakeGeneratedMediaStore:
    def __init__(self):
        self.uploads = 0
        self.fail_probe = False
        self.interrupt = False
        self.media_objects = {}
        self.inspections = 0

    def check_health(self):
        return {"status": "healthy"}

    def get_capabilities(self):
        return {"render": True}

    def upload_media(self, project_id, filename, content_type, stream):
        payload = stream.read()
        if self.interrupt:
            raise RuntimeError("stream interrupted")
        self.uploads += 1
        metadata = {"durationSeconds": 1.0, "sizeBytes": len(payload)}
        if not self.fail_probe:
            metadata["video"] = {"codec": "h264", "width": 320, "height": 240}
        media_id = f"generated-media-{self.uploads}"
        self.media_objects[(project_id, media_id)] = {
            "content": payload,
            "metadata": metadata,
            "contentType": content_type,
        }
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": metadata,
        }

    def inspect_media(self, project_id, media_id):
        self.inspections += 1
        stored = self.media_objects.get((project_id, media_id))
        if stored is None:
            raise VideoUseNotFoundError("media not found")
        payload = stored["content"]
        return {
            "projectId": project_id,
            "mediaId": media_id,
            "mediaType": "video",
            "contentType": stored["contentType"],
            "sizeBytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "metadata": stored["metadata"],
        }


@pytest.fixture()
def generation_context(tmp_path, monkeypatch):
    monkeypatch.setenv("AETHER_MAX_UPLOAD_BYTES", "1024")
    engine = build_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    media_store = FakeGeneratedMediaStore()
    app = create_app(
        app_engine=engine,
        db_dependency=get_db,
        video_use_adapter=media_store,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    app.state.moneyprinter = MagicMock()
    with LeaseAwareTestClient(app) as client:
        assert client.post("/auth/login", json={"email": "owner@example.com", "password": OWNER_PASSWORD}).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, sessions, app, media_store
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def provider_control_context(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'provider-control.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        with sessions() as db:
            yield db

    app = create_app(
        app_engine=engine,
        db_dependency=get_db,
        video_use_adapter=FakeGeneratedMediaStore(),
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="provider-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="moneyprinter",
    )
    with LeaseAwareTestClient(app) as client:
        assert client.post(
            "/auth/login",
            json={"email": "provider-owner@example.com", "password": OWNER_PASSWORD},
        ).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, sessions, app, engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def provider_policy(**overrides):
    policy = {
        "enabledIntent": True,
        "allowedAspects": ["16:9", "9:16", "1:1"],
        "allowedVoices": ["en-US-JennyNeural"],
        "allowedConcatModes": ["random", "sequential"],
        "maxClipDurationSeconds": 10,
        "maxOutputs": 1,
        "concurrentTaskLimit": 1,
        "monthlyRequestLimit": 1,
        "monthlyGeneratedSecondsLimit": 10,
        "failureWindow": 300,
        "failureThreshold": 2,
        "cooldownSeconds": 60,
        "artifactPathPrefixes": ["/tasks/"],
        "maxArtifactBytes": 1024,
        "configLabel": "provider-readiness-v1",
        "description": "Governed activation policy.",
    }
    policy.update(overrides)
    return policy


def create_provider_config(client, **overrides):
    response = client.post(
        "/generation/providers/moneyprinter/config-versions",
        json=provider_policy(**overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


def publish_provider_config(client, config):
    response = client.post(
        f"/generation/providers/moneyprinter/config-versions/{config['id']}/publish"
    )
    assert response.status_code == 200, response.text
    return response.json()


def attest_provider(client, sessions, config, **overrides):
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "provider": "moneyprinter",
        "operatorMode": "moneyprinter",
        "tenantId": tenant_id,
        "configVersionId": config["id"],
        "policyHash": config["policyHash"],
        "adapterVersion": "aether-moneyprinter-v2",
        "upstreamPin": "475f21147f0808f5ffe3f58af9ab794b28a4da2c",
        "healthy": True,
        "credentialState": "PRESENT",
        "networkIsolation": "ENFORCED",
        "canaryProfile": "private-one-task-v1",
        "capabilities": {
            "videoAspects": ["9:16"],
            "voices": ["en-US-JennyNeural"],
            "videoConcatModes": ["random"],
            "maxOutputs": 1,
            "maxClipDurationSeconds": 10,
            "cancellationSupported": False,
            "artifactStreaming": True,
        },
        "reasonCode": None,
        "checkedAt": now.isoformat(),
        "expiresAt": (now + datetime.timedelta(minutes=4)).isoformat(),
    }
    payload.update(overrides)
    return client.post(
        "/internal/generation/providers/moneyprinter/attest",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-proof"},
        json=payload,
    )


def create_project(client, name="Generation project"):
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201
    return response.json()


def idempotency_uuid(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def generation_request(client, project, idempotency_key="default-generation-key", **overrides):
    capabilities = client.get("/generation/providers/moneyprinter/capabilities").json()
    payload = {
        "videoSubject": "A horse crosses a rainy city",
        "videoAspect": "9:16",
        "voiceName": "en-US-JennyNeural",
        "videoConcatMode": "random",
        "videoClipDuration": 5,
        "outputCount": 1,
        "inputAssetVersionIds": [],
        "idempotencyKey": idempotency_uuid(idempotency_key),
        "capabilitySnapshotHash": capabilities["snapshotHash"],
        "expectedProjectRevision": project["revision"],
        "confirmExternalGeneration": True,
    }
    payload.update(overrides)
    return payload


def create_task(client, project, key="generation-key-0001", **overrides):
    key_uuid = idempotency_uuid(key)
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        headers={"Idempotency-Key": key_uuid},
        json=generation_request(client, project, idempotency_key=key, **overrides),
    )
    assert response.status_code == 202, response.text
    return response.json()


def claim(client, worker_id="worker-a"):
    return client.post(
        "/internal/generation-tasks/claim",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
    )


def transition(client, task_id, status, worker_id="worker-a", **values):
    payload = {
        "status": status,
        "progress": values.pop("progress", 10),
        "message": values.pop("message", status),
        **values,
    }
    return client.post(
        f"/internal/generation-tasks/{task_id}/transition",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
        json=payload,
    )


def ingest_success(client, project, key="generation-key-ingest"):
    task = create_task(client, project, key=key)
    claimed = claim(client).json()
    assert claimed["taskId"] == task["taskId"]
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream-1").status_code == 200
    assert transition(
        client, task["taskId"], "INGESTING", progress=95,
        upstreamJobId="upstream-1", providerArtifactId="artifact-1",
    ).status_code == 200
    response = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact-1"},
        files={"file": ("artifact.mp4", b"deterministic-video", "video/mp4")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_01_owner_create_is_202_and_never_calls_provider(generation_context):
    client, _, app, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    assert task["status"] == "QUEUED"
    app.state.moneyprinter.assert_not_called()


def test_02_same_idempotency_and_body_returns_same_task(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    first = create_task(client, project, key="same-request-key")
    second = create_task(client, project, key="same-request-key")
    assert second["taskId"] == first["taskId"]


def test_03_same_idempotency_different_body_conflicts(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    create_task(client, project, key="conflicting-key")
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        headers={"Idempotency-Key": idempotency_uuid("conflicting-key")},
        json=generation_request(client, project, idempotency_key="conflicting-key", videoSubject="different"),
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_04_viewer_is_read_only_with_zero_writes(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    password = secrets.token_urlsafe(24)
    assert client.post("/admin/users", json={
        "email": "viewer@example.com", "displayName": "Viewer", "password": password, "role": "viewer",
    }).status_code == 201
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"email": "viewer@example.com", "password": password}).status_code == 200
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        json=generation_request(client, project),
    )
    assert response.status_code == 403
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0


def test_05_disabled_provider_blocks_without_writes(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'disabled.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    def get_db():
        with sessions() as db:
            yield db
    app = create_app(
        app_engine=engine, db_dependency=get_db,
        bootstrap_admin_password=OWNER_PASSWORD, bootstrap_admin_email="disabled@example.com",
        cookie_secure=False, enforce_csrf=True,
    )
    with LeaseAwareTestClient(app) as client:
        client.post("/auth/login", json={"email": "disabled@example.com", "password": OWNER_PASSWORD})
        client.headers.update({"X-Aether-CSRF": "1"})
        project = create_project(client)
        request = generation_request(client, project)
        response = client.post(
            f"/projects/{project['id']}/generation-tasks",
            json=request,
        )
        assert response.status_code == 503
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("videoSubject", ""),
        ("videoAspect", "4:3"),
        ("videoClipDuration", 0),
        ("videoClipDuration", 11),
        ("outputCount", 2),
        ("confirmExternalGeneration", False),
        ("voiceName", "unknown-voice"),
    ],
)
def test_06_to_12_strict_request_and_capability_validation(generation_context, field, value):
    client, sessions, _, _ = generation_context
    project = create_project(client, f"invalid-{field}-{value}")
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/validate",
        json=generation_request(client, project, **{field: value}),
    )
    assert response.status_code in {409, 422}
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0


def test_13_unknown_capability_and_revision_conflict_are_rejected(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    unknown = generation_request(client, project, capabilitySnapshotHash="0" * 64)
    assert client.post(f"/projects/{project['id']}/generation-tasks/validate", json=unknown).status_code == 409
    stale = generation_request(client, project, expectedProjectRevision=999)
    assert client.post(f"/projects/{project['id']}/generation-tasks/validate", json=stale).status_code == 409


def test_14_list_detail_are_scoped_paginated_and_prompt_free(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    listing = client.get(f"/projects/{project['id']}/generation-tasks?pageSize=1").json()
    assert listing["items"][0]["taskId"] == task["taskId"]
    assert "A horse" not in str(listing)
    detail = client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").json()
    assert len(detail["attempts"]) == 1
    assert "videoSubject" not in str(detail)


def test_15_csrf_protects_generation_mutations(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    del client.headers["X-Aether-CSRF"]
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        json=generation_request(client, project),
    )
    assert response.status_code == 403


def test_16_internal_claim_requires_token_and_claims_once(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    assert client.post("/internal/generation-tasks/claim").status_code == 401
    first = claim(client)
    assert first.status_code == 200 and first.json()["taskId"] == task["taskId"]
    assert claim(client, "worker-b").status_code == 204


def test_17_heartbeat_requires_current_lease_owner(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    assert client.post(
        f"/internal/generation-tasks/{task['taskId']}/heartbeat",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-b"}, json={},
    ).status_code == 409
    assert client.post(
        f"/internal/generation-tasks/{task['taskId']}/heartbeat",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"}, json={},
    ).status_code == 200


def test_18_upstream_id_is_immutable_and_illegal_transition_rejected(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream-a").status_code == 200
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream-b").status_code == 409
    assert transition(client, task["taskId"], "SUBMITTING").status_code == 409


def test_19_cancel_is_idempotent_and_late_success_isolated(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    path = f"/projects/{project['id']}/generation-tasks/{task['taskId']}/cancel"
    assert client.post(path).json()["status"] == "CANCELED"
    assert client.post(path).json()["status"] == "CANCELED"
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="late").status_code == 409


def test_20_retry_appends_attempt_and_preserves_history(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    failed = transition(
        client, task["taskId"], "FAILED", errorCode="TEMPORARY", errorMessage="retry", retryable=True,
    )
    assert failed.status_code == 200
    retried = client.post(f"/projects/{project['id']}/generation-tasks/{task['taskId']}/retry")
    assert retried.status_code == 202
    assert retried.json()["attempt"] == 2
    with sessions() as db:
        attempts = list(db.execute(select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task["taskId"])).scalars())
        assert [attempt.attempt_no for attempt in attempts] == [1, 2]


def test_21_unknown_submission_cannot_blindly_retry(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    assert transition(client, task["taskId"], "UNKNOWN", errorCode="AMBIGUOUS_SUBMISSION", errorMessage="timeout").status_code == 200
    response = client.post(f"/projects/{project['id']}/generation-tasks/{task['taskId']}/retry")
    assert response.status_code == 409


def test_22_expired_lease_is_recovered_without_new_attempt(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    assert transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream-recover").status_code == 200
    with sessions() as db:
        row = db.get(DBGenerationTask, task["taskId"])
        row.lease_expires_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        db.commit()
    recovered = claim(client, "worker-b")
    assert recovered.status_code == 200
    assert recovered.json()["upstreamJobId"] == "upstream-recover"
    assert recovered.json()["attempt"] == 1


@pytest.mark.parametrize(
    ("data", "files", "expected"),
    [
        ({"providerArtifactId": "https://evil.example/a.mp4"}, {"file": ("a.mp4", b"x", "video/mp4")}, 422),
        ({"providerArtifactId": "../escape"}, {"file": ("a.mp4", b"x", "video/mp4")}, 422),
        ({"providerArtifactId": "artifact"}, {"file": ("a.mp4", b"", "video/mp4")}, 422),
        ({"providerArtifactId": "artifact"}, {"file": ("a.txt", b"x", "text/plain")}, 415),
        ({"providerArtifactId": "artifact"}, {"file": ("a.mp4", b"x" * 1025, "video/mp4")}, 413),
    ],
)
def test_23_to_27_artifact_intake_rejects_untrusted_or_invalid_payloads(generation_context, data, files, expected):
    client, _, _, _ = generation_context
    project = create_project(client, f"invalid-artifact-{expected}")
    task = create_task(client, project, key=f"artifact-invalid-{expected}-{len(files['file'][1])}")
    claim(client)
    transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream")
    transition(client, task["taskId"], "INGESTING", upstreamJobId="upstream", providerArtifactId="artifact")
    response = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data=data, files=files,
    )
    assert response.status_code == expected


def test_28_json_or_unauthenticated_artifact_intake_is_rejected(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    assert client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        json={"url": "https://evil.example/a.mp4"},
    ).status_code == 422
    assert client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        data={"providerArtifactId": "artifact"},
        files={"file": ("artifact.mp4", b"video", "video/mp4")},
    ).status_code == 401


def test_29_probe_failure_compensates_quota_and_creates_no_asset(generation_context):
    client, sessions, _, media_store = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream")
    transition(client, task["taskId"], "INGESTING", upstreamJobId="upstream", providerArtifactId="artifact")
    media_store.fail_probe = True
    response = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact"},
        files={"file": ("a.mp4", b"video", "video/mp4")},
    )
    assert response.status_code == 422
    with sessions() as db:
        tenant = db.execute(select(DBTenant)).scalar_one()
        assert tenant.used_storage_bytes == 0
        assert db.execute(select(func.count(DBAssetVersion.id))).scalar_one() == 0



def test_g72_upload_failure_compensates_storage_with_portable_case(generation_context, monkeypatch):
    """A sidecar upload failure releases only the committed storage reservation."""
    client, sessions, app, media_store = generation_context
    project = create_project(client, "G72 storage compensation")
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-storage-compensation")
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    created = client.post(path, json=payload)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]

    claimed = claim(client, worker_id="worker-g72-compensation")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    assert transition(
        client, task_id, "RUNNING", worker_id="worker-g72-compensation",
        upstreamJobId="g72-compensation-upstream",
    ).status_code == 200
    assert transition(
        client, task_id, "INGESTING", worker_id="worker-g72-compensation",
        upstreamJobId="g72-compensation-upstream",
        providerArtifactId="g72-compensation-artifact",
    ).status_code == 200

    def fail_upload(*_args, **_kwargs):
        raise VideoUseError("synthetic sidecar upload failure")

    monkeypatch.setattr(media_store, "upload_media", fail_upload)
    response = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={
            "X-Worker-Token": WORKER_TOKEN,
            "X-Worker-Id": "worker-g72-compensation",
        },
        data={"providerArtifactId": "g72-compensation-artifact"},
        files={"file": ("synthetic.mp4", b"synthetic-g72-bytes", "video/mp4")},
    )
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "ARTIFACT_STORE_FAILED"

    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        tenant = db.get(DBTenant, task.tenant_id)
        assert task.status == "FAILED"
        assert task.error_code == "ARTIFACT_INGEST_FAILED"
        assert tenant.used_storage_bytes == 0
        assert db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project["id"],
            DBAssetVersion.media_type == "video",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
        )).scalar_one() == 1
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        assert attempt.reconciliation_state == "HELD"
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()

def test_30_success_creates_one_material_and_immutable_asset_with_provenance(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = ingest_success(client, project)
    assert task["storedStatus"] == "RIGHTS_BLOCKED"
    result = task["results"][0]
    assert result["provenance"]["generationTaskId"] == task["taskId"]
    with sessions() as db:
        stored_project = db.get(DBProject, project["id"])
        assert len(stored_project.materials) == 1
        assert db.execute(select(func.count(DBAssetVersion.id))).scalar_one() == 1
        assert db.execute(select(func.count(DBCandidate.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBAdoption.id))).scalar_one() == 0
        assert stored_project.timeline["tracks"] == []


def test_generated_media_download_requires_current_export_rights(generation_context, monkeypatch):
    client, sessions, app, media_store = generation_context
    project = create_project(client, "generated-media-export-rights")
    task = ingest_success(client, project)
    result = task["results"][0]
    media_id = result["mediaId"]
    asset_id = result["assetVersionId"]
    payload = media_store.media_objects[(project["id"], media_id)]["content"]
    stream_calls = []

    class FakeUpstream:
        def __init__(self, body):
            self.body = body
            self.status_code = 200
            self.headers = {
                "content-type": "video/mp4",
                "content-length": str(len(body)),
            }

        def iter_bytes(self):
            yield self.body

    class FakeStreamContext:
        def __init__(self, body):
            self.response = FakeUpstream(body)

        def __enter__(self):
            return self.response

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_stream(path, headers=None):
        stream_calls.append((path, headers))
        return FakeStreamContext(payload)

    monkeypatch.setattr(app.state.video_use, "stream", fake_stream, raising=False)
    media_url = f"/video-use/media/{project['id']}/{media_id}"

    missing = client.get(media_url)
    assert missing.status_code == 403
    assert missing.json()["detail"]["code"] == "RIGHTS_MISSING"
    assert stream_calls == []

    rights_url = f"/projects/{project['id']}/asset-versions/{asset_id}/rights-snapshots"
    owner_allowed = client.post(
        rights_url,
        json={"status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL", "evidenceRef": "owner-self-asserted"},
    )
    assert owner_allowed.status_code == 201, owner_allowed.text
    owner_denied = client.get(media_url)
    assert owner_denied.status_code == 403
    assert owner_denied.json()["detail"]["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    owner_rights_check = client.get(
        f"/projects/{project['id']}/asset-versions/{asset_id}/rights-check",
        params={"purpose": "EXPORT"},
    ).json()
    assert owner_rights_check["allowed"] is False
    assert owner_rights_check["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    owner_default_rights_check = client.get(
        f"/projects/{project['id']}/asset-versions/{asset_id}/rights-check"
    ).json()
    assert owner_default_rights_check["allowed"] is False
    assert owner_default_rights_check["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    assert stream_calls == []

    editor_password = secrets.token_urlsafe(24)
    created_editor = client.post(
        "/admin/users",
        json={
            "email": "generated-export-editor@example.com",
            "displayName": "Generated Export Editor",
            "password": editor_password,
            "role": "editor",
        },
    )
    assert created_editor.status_code == 201, created_editor.text
    assert client.post("/auth/logout").status_code == 204
    editor_login = client.post(
        "/auth/login",
        json={"email": "generated-export-editor@example.com", "password": editor_password},
    )
    assert editor_login.status_code == 200, editor_login.text

    editor_allowed = client.post(
        rights_url,
        json={"status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL", "evidenceRef": "editor-self-asserted"},
    )
    assert editor_allowed.status_code == 201, editor_allowed.text
    editor_denied = client.get(media_url)
    assert editor_denied.status_code == 403
    assert editor_denied.json()["detail"]["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    editor_rights_check = client.get(
        f"/projects/{project['id']}/asset-versions/{asset_id}/rights-check",
        params={"purpose": "EXPORT"},
    ).json()
    assert editor_rights_check["allowed"] is False
    assert editor_rights_check["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    assert stream_calls == []

    revoked = client.post(
        rights_url,
        json={"status": "REVOKED", "purpose": "EXPORT", "territory": "GLOBAL", "evidenceRef": "editor-revoked"},
    )
    assert revoked.status_code == 201, revoked.text
    denied = client.get(media_url)
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "RIGHTS_REVOKED"
    assert stream_calls == []


def test_31_repeated_artifact_completion_is_idempotent_without_double_quota(generation_context):
    client, sessions, _, media_store = generation_context
    project = create_project(client)
    task = ingest_success(client, project)
    with sessions() as db:
        used = db.execute(select(DBTenant.used_storage_bytes)).scalar_one()
    response = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact-1"},
        files={"file": ("artifact.mp4", b"deterministic-video", "video/mp4")},
    )
    assert response.status_code == 201
    with sessions() as db:
        assert db.execute(select(DBTenant.used_storage_bytes)).scalar_one() == used
        assert db.execute(select(func.count(DBAssetVersion.id))).scalar_one() == 1
    assert media_store.uploads == 1


def test_32_generated_rights_require_trusted_issuer_and_preserve_evidence_status(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = ingest_success(client, project)
    asset_id = task["results"][0]["assetVersionId"]
    assert task["rights"] == {"allowed": False, "code": "RIGHTS_MISSING"}
    rights_path = f"/projects/{project['id']}/asset-versions/{asset_id}/rights-snapshots"
    for status in ("DENIED", "REVOKED"):
        response = client.post(
            rights_path,
            json={"status": status, "purpose": "EXPORT", "territory": "GLOBAL", "evidenceRef": f"evidence-{status.lower()}"},
        )
        assert response.status_code == 201
        blocked = client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").json()
        assert blocked["rights"] == {"allowed": False, "code": f"RIGHTS_{status}"}
    now = datetime.datetime.now(datetime.timezone.utc)
    not_yet = client.post(
        rights_path,
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now + datetime.timedelta(days=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=2)).isoformat(),
        },
    )
    assert not_yet.status_code == 201
    assert client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").json()["rights"]["code"] == "RIGHTS_NOT_YET_VALID"
    expired = client.post(
        rights_path,
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(days=2)).isoformat(),
            "validUntil": (now - datetime.timedelta(days=1)).isoformat(),
        },
    )
    assert expired.status_code == 201
    assert client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").json()["rights"]["code"] == "RIGHTS_EXPIRED"
    response = client.post(
        f"/projects/{project['id']}/asset-versions/{asset_id}/rights-snapshots",
        json={
            "status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL",
            "validFrom": (now - datetime.timedelta(minutes=1)).isoformat(),
            "validUntil": (now + datetime.timedelta(days=1)).isoformat(),
            "evidenceRef": "evidence-1",
        },
    )
    assert response.status_code == 201
    refreshed = client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").json()
    assert refreshed["rights"] == {
        "allowed": False,
        "code": "RIGHTS_TRUSTED_ISSUER_REQUIRED",
    }
    assert refreshed["status"] == "RIGHTS_BLOCKED"
    assert refreshed["storedStatus"] == "RIGHTS_BLOCKED"


def test_33_generation_events_are_append_only(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    create_task(client, project)
    with sessions() as db:
        event = db.execute(select(DBGenerationEvent)).scalars().first()
        event.event_type = "MUTATED"
        with pytest.raises(ValueError, match="immutable"):
            db.commit()
        db.rollback()


def test_34_concurrent_claim_has_single_winner(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    create_task(client, project)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda worker: claim(client, worker).status_code, ["worker-a", "worker-b"]))
    assert sorted(responses) == [200, 204]


def test_35_generation_tables_are_additive_and_attempt_event_constraints_exist(generation_context):
    _, sessions, _, _ = generation_context
    with sessions() as db:
        table_names = set(db.bind.dialect.get_table_names(db.connection()))
    assert {"generation_tasks", "generation_attempts", "generation_events"}.issubset(table_names)


def test_36_browser_dto_never_contains_secrets_or_raw_provider_response(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project, videoSubject="secret token bearer api_key")
    serialized = str(task).lower()
    assert "videoSubject".lower() not in serialized
    assert "api_key" not in serialized
    assert "rawresponse" not in serialized


def test_37_tenant_scope_hides_foreign_task(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    now = datetime.datetime.now(datetime.timezone.utc)
    password = secrets.token_urlsafe(24)
    with sessions() as db:
        tenant = DBTenant(
            id="tenant-foreign", name="Foreign", slug="foreign", project_quota=50,
            storage_quota_bytes=1024**3, used_storage_bytes=0, concurrent_render_quota=2,
            monthly_render_seconds_quota=3600, render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"), created_at=now, updated_at=now,
        )
        user = DBUser(
            id="user-foreign", tenant_id=tenant.id, email="foreign@example.com",
            display_name="Foreign", password_hash=hash_password(password), role="owner",
            is_active=True, created_at=now, updated_at=now,
        )
        db.add_all([tenant, user])
        db.commit()
    client.post("/auth/logout")
    client.post("/auth/login", json={"email": "foreign@example.com", "password": password})
    assert client.get(f"/projects/{project['id']}/generation-tasks/{task['taskId']}").status_code == 404


def test_38_storage_quota_rejects_before_media_store(generation_context):
    client, sessions, _, media_store = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream")
    transition(client, task["taskId"], "INGESTING", upstreamJobId="upstream", providerArtifactId="artifact")
    with sessions() as db:
        tenant = db.execute(select(DBTenant)).scalar_one()
        tenant.storage_quota_bytes = 1
        db.commit()
    response = client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact"}, files={"file": ("a.mp4", b"video", "video/mp4")},
    )
    assert response.status_code == 429
    assert media_store.uploads == 0


def test_39_non_retryable_provider_4xx_cannot_be_retried(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    transition(client, task["taskId"], "FAILED", errorCode="PROVIDER_400", errorMessage="bad request", retryable=False)
    response = client.post(f"/projects/{project['id']}/generation-tasks/{task['taskId']}/retry")
    assert response.status_code == 409


def test_40_stream_interruption_compensates_quota_and_records_failure(generation_context):
    client, sessions, _, media_store = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream")
    transition(client, task["taskId"], "INGESTING", upstreamJobId="upstream", providerArtifactId="artifact")
    media_store.interrupt = True
    with pytest.raises(RuntimeError, match="stream interrupted"):
        client.post(
            f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
            data={"providerArtifactId": "artifact"},
            files={"file": ("artifact.mp4", b"video", "video/mp4")},
        )
    with sessions() as db:
        tenant = db.execute(select(DBTenant)).scalar_one()
        stored = db.get(DBGenerationTask, task["taskId"])
        assert tenant.used_storage_bytes == 0
        assert stored.status == "FAILED"
        assert db.execute(select(func.count(DBAssetVersion.id))).scalar_one() == 0




def test_g71_ambiguous_artifact_ingest_failed_cannot_reissue_paid_generation(generation_context):
    """G70/G71: original upstream output must be reconciled, never regenerated blindly.

    FAKE-only fixture: temporarily model the 3-attempt paid-provider retry policy.
    No external provider, credentials or paid call is involved.
    """
    client, sessions, app, media_store = generation_context
    project = create_project(client, "g71-no-double-generation")
    task = create_task(client, project, key="g71-ingest-ambiguous")
    task_id = task["taskId"]
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        stored.max_attempts = 3
        db.commit()

    assert claim(client).status_code == 200
    assert transition(client, task_id, "RUNNING", upstreamJobId="original-paid-job").status_code == 200
    assert transition(
        client, task_id, "INGESTING", upstreamJobId="original-paid-job",
        providerArtifactId="original-paid-artifact",
    ).status_code == 200
    media_store.interrupt = True
    with pytest.raises(RuntimeError, match="stream interrupted"):
        client.post(
            f"/internal/generation-tasks/{task_id}/artifact-intake",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
            data={"providerArtifactId": "original-paid-artifact"},
            files={"file": ("artifact.mp4", b"synthetic-bytes", "video/mp4")},
        )
    with sessions() as db:
        row = db.get(DBGenerationTask, task_id)
        assert row.status == "FAILED"
        assert row.error_code == "ARTIFACT_INGEST_FAILED"
        assert row.upstream_job_id == "original-paid-job"
        assert row.provider_artifact_id == "original-paid-artifact"
        attempts_before = db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one()
        events_before = db.execute(select(func.count(DBGenerationEvent.id))).scalar_one()
        usage_before = db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one()
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        assert attempt.reconciliation_state == "HELD"
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0

    blocked = client.post(f"/projects/{project['id']}/generation-tasks/{task_id}/retry")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
    with sessions() as db:
        row = db.get(DBGenerationTask, task_id)
        assert row.status == "FAILED"
        assert row.attempts == 1
        assert row.upstream_job_id == "original-paid-job"
        assert row.provider_artifact_id == "original-paid-artifact"
        assert db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one() == attempts_before == 1
        assert db.execute(select(func.count(DBGenerationEvent.id))).scalar_one() == events_before
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == usage_before
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        assert attempt.reconciliation_state == "HELD"
    app.state.moneyprinter.assert_not_called()


def test_g71_artifact_stream_failure_holds_reservation_and_blocks_new_paid_attempt(generation_context):
    client, sessions, app, _ = generation_context
    project = create_project(client, "g71-stream-failure-hold")
    task = create_task(client, project, key="g71-stream-failure-hold")
    task_id = task["taskId"]
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        stored.max_attempts = 3
        db.commit()

    assert claim(client).status_code == 200
    assert transition(client, task_id, "RUNNING", upstreamJobId="original-paid-job").status_code == 200
    assert transition(
        client, task_id, "INGESTING", upstreamJobId="original-paid-job",
        providerArtifactId="original-paid-artifact",
    ).status_code == 200
    failed = transition(
        client, task_id, "FAILED", upstreamJobId="original-paid-job",
        errorCode="ARTIFACT_STREAM_FAILED", errorMessage="synthetic stream interruption",
        retryable=False,
    )
    assert failed.status_code == 200
    with sessions() as db:
        row = db.get(DBGenerationTask, task_id)
        attempt = db.execute(
            select(DBGenerationAttempt).where(DBGenerationAttempt.generation_task_id == task_id)
        ).scalar_one()
        assert row.status == "FAILED"
        assert row.error_code == "ARTIFACT_INGEST_FAILED"
        assert row.upstream_job_id == "original-paid-job"
        assert row.provider_artifact_id == "original-paid-artifact"
        assert attempt.reconciliation_state == "HELD"
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0

    blocked = client.post(f"/projects/{project['id']}/generation-tasks/{task_id}/retry")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "ARTIFACT_RECOVERY_REQUIRED"
    with sessions() as db:
        row = db.get(DBGenerationTask, task_id)
        assert row.attempts == 1
        assert row.upstream_job_id == "original-paid-job"
        assert row.provider_artifact_id == "original-paid-artifact"
        assert db.execute(select(func.count(DBGenerationAttempt.id)).where(
            DBGenerationAttempt.generation_task_id == task_id
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RELEASED",
        )).scalar_one() == 0
    app.state.moneyprinter.assert_not_called()



def seed_g71_orphan_artifact(
    sessions,
    media_store,
    project_id,
    task_id,
    *,
    include_sidecar_object=True,
    expected_sha256=None,
    include_orphan_reference=True,
):
    payload = b"synthetic-original-orphan-video"
    media_id = f"orphan-media-{task_id[:8]}"
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "durationSeconds": 1.0,
        "sizeBytes": len(payload),
        "video": {"codec": "h264", "width": 320, "height": 240},
        "audio": None,
    }
    if include_sidecar_object:
        media_store.media_objects[(project_id, media_id)] = {
            "content": payload,
            "metadata": metadata,
            "contentType": "video/mp4",
        }
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        attempt = db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        task.status = "FAILED"
        task.progress = 100
        task.error_code = "ARTIFACT_INGEST_FAILED"
        task.error_message = "synthetic original artifact transfer failure"
        task.upstream_job_id = "original-upstream-job"
        task.provider_artifact_id = "original-provider-artifact"
        task.media_id = media_id if include_orphan_reference else None
        task.completed_at = now
        task.updated_at = now
        task.lease_owner = None
        task.lease_expires_at = None
        attempt.status = "FAILED"
        attempt.upstream_job_id = "original-upstream-job"
        attempt.reconciliation_state = "HELD"
        attempt.completed_at = now
        event_metadata = {
            "originalUpstreamJobId": "original-upstream-job",
            "originalProviderArtifactId": "original-provider-artifact",
            "reconciliationState": "HELD",
            "usageReservation": "HELD",
        }
        if include_orphan_reference:
            event_metadata.update({
                "orphanedMediaId": media_id,
                "orphanedMediaSha256": expected_sha256 or digest,
                "orphanedMediaSizeBytes": len(payload),
                "orphanedMediaMetadata": metadata,
            })
        db.add(DBGenerationEvent(
            id=str(uuid.uuid4()),
            generation_task_id=task_id,
            attempt_id=attempt.id,
            event_type="ARTIFACT_INGEST_FAILED",
            from_status="INGESTING",
            to_status="FAILED",
            actor_type="SYSTEM",
            actor_id="api",
            metadata_json=event_metadata,
            created_at=now,
        ))
        db.commit()
    return media_id, payload


def test_g70_g71_owner_recovers_verified_orphan_without_duplicate_upload_or_attempt(generation_context):
    client, sessions, app, media_store = generation_context
    project = create_project(client, "g70-owner-orphan-recovery")
    task = create_task(client, project, key="g70-owner-orphan-recovery")
    task_id = task["taskId"]
    media_id, payload = seed_g71_orphan_artifact(
        sessions, media_store, project["id"], task_id
    )

    url = f"/projects/{project['id']}/generation-tasks/{task_id}/recover-artifact"
    recovered = client.post(
        url,
        json={"reason": "Owner verified original job and orphan media receipt"},
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "RIGHTS_BLOCKED"
    assert recovered.json()["results"][0]["mediaId"] == media_id

    # Repeating the same Owner action is idempotent: no second asset, event,
    # storage reservation, usage settlement, upload or generation attempt.
    repeated = client.post(
        url,
        json={"reason": "Owner repeated recovery request for idempotency check"},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["results"][0]["assetVersionId"] == recovered.json()["results"][0]["assetVersionId"]

    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.execute(select(DBTenant)).scalar_one()
        project_row = db.get(DBProject, project["id"])
        assets = list(db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.project_id == project["id"],
                DBAssetVersion.media_id == media_id,
            )
        ).scalars())
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task_id
            )
        ).scalars())
        attempts = list(db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalars())
        receipts = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        assert stored.status == "RIGHTS_BLOCKED"
        assert stored.error_code is None
        assert stored.upstream_job_id == "original-upstream-job"
        assert stored.provider_artifact_id == "original-provider-artifact"
        assert stored.attempts == 1
        assert len(attempts) == 1
        assert len(assets) == 1
        assert assets[0].sha256 == hashlib.sha256(payload).hexdigest()
        assert assets[0].size_bytes == len(payload)
        assert sum(1 for row in usage if row.kind == "RESERVED") == 1
        assert sum(1 for row in usage if row.kind == "SETTLED") == 0
        assert sum(1 for row in usage if row.kind == "RELEASED") == 0
        assert receipts == 1
        receipt = db.execute(
            select(DBGenerationEvent).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        assert receipt.actor_type == "OWNER"
        assert receipt.actor_id == stored.requested_by
        assert receipt.metadata_json["reason"] == "Owner verified original job and orphan media receipt"
        assert receipt.metadata_json["usageReservationState"] == "RESERVED_HELD"
        assert receipt.metadata_json["m11M13Reconciliation"] == "PENDING"
        assert tenant.used_storage_bytes == len(payload)
        assert sum(1 for item in project_row.materials if item.get("id") == media_id) == 1
    assert media_store.uploads == 0
    assert media_store.inspections == 1
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_owner_recovery_fails_closed_when_original_media_is_absent(generation_context):
    client, sessions, app, media_store = generation_context
    project = create_project(client, "g70-owner-original-fetch-blocked")
    task = create_task(client, project, key="g70-owner-original-fetch-blocked")
    task_id = task["taskId"]
    seed_g71_orphan_artifact(
        sessions,
        media_store,
        project["id"],
        task_id,
        include_sidecar_object=False,
        include_orphan_reference=True,
    )
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/{task_id}/recover-artifact",
        json={"reason": "Owner checked original job but sidecar media is absent"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        attempt = db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task_id
            )
        ).scalars())
        assert stored.status == "FAILED"
        assert stored.error_code == "ARTIFACT_INGEST_FAILED"
        assert attempt.reconciliation_state == "HELD"
        assert sum(1 for row in usage if row.kind == "RESERVED") == 1
        assert sum(1 for row in usage if row.kind in {"RELEASED", "SETTLED"}) == 0
    assert media_store.uploads == 0
    app.state.moneyprinter.assert_not_called()


def test_g70_g71_owner_recovery_rejects_orphan_hash_mismatch(generation_context):
    client, sessions, app, media_store = generation_context
    project = create_project(client, "g70-owner-orphan-hash-mismatch")
    task = create_task(client, project, key="g70-owner-orphan-hash-mismatch")
    task_id = task["taskId"]
    seed_g71_orphan_artifact(
        sessions,
        media_store,
        project["id"],
        task_id,
        expected_sha256="0" * 64,
    )
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/{task_id}/recover-artifact",
        json={"reason": "Owner checks hash mismatch isolation behavior"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ORPHAN_MEDIA_INTEGRITY_MISMATCH"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        tenant = db.execute(select(DBTenant)).scalar_one()
        assets = db.execute(
            select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == project["id"]
            )
        ).scalar_one()
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task_id
            )
        ).scalars())
        assert stored.status == "FAILED"
        assert stored.error_code == "ARTIFACT_INGEST_FAILED"
        assert tenant.used_storage_bytes == 0
        assert assets == 0
        assert sum(1 for row in usage if row.kind == "RESERVED") == 1
        assert sum(1 for row in usage if row.kind in {"RELEASED", "SETTLED"}) == 0
    assert media_store.uploads == 0
    app.state.moneyprinter.assert_not_called()



def test_g70_g71_recovery_rejects_admin_without_delegation_and_foreign_tenant_owner(generation_context):
    client, sessions, app, media_store = generation_context
    project = create_project(client, "g70-recovery-owner-and-tenant-scope")
    task = create_task(client, project, key="g70-recovery-owner-and-tenant-scope")
    task_id = task["taskId"]
    seed_g71_orphan_artifact(sessions, media_store, project["id"], task_id)

    now = datetime.datetime.now(datetime.timezone.utc)
    admin_password = secrets.token_urlsafe(24)
    same_tenant_owner_password = secrets.token_urlsafe(24)
    foreign_password = secrets.token_urlsafe(24)
    with sessions() as db:
        tenant_id = db.get(DBProject, project["id"]).tenant_id
        db.add_all([
            DBUser(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                email="g70-recovery-admin-no-delegation@example.com",
                display_name="G70 Recovery Admin Without Delegation",
                password_hash=hash_password(admin_password),
                role="admin",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
            DBUser(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                email="g70-recovery-other-owner@example.com",
                display_name="G70 Recovery Other Same-Tenant Owner",
                password_hash=hash_password(same_tenant_owner_password),
                role="owner",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
        ])
        foreign_tenant = DBTenant(
            id="g70-recovery-foreign-tenant",
            name="G70 Recovery Foreign Tenant",
            slug="g70-recovery-foreign-tenant",
            project_quota=50,
            storage_quota_bytes=1024**3,
            used_storage_bytes=0,
            concurrent_render_quota=2,
            monthly_render_seconds_quota=3600,
            render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"),
            created_at=now,
            updated_at=now,
        )
        db.add_all([
            foreign_tenant,
            DBUser(
                id=str(uuid.uuid4()),
                tenant_id=foreign_tenant.id,
                email="g70-recovery-foreign-owner@example.com",
                display_name="G70 Recovery Foreign Owner",
                password_hash=hash_password(foreign_password),
                role="owner",
                is_active=True,
                created_at=now,
                updated_at=now,
            ),
        ])
        db.commit()

    url = f"/projects/{project['id']}/generation-tasks/{task_id}/recover-artifact"
    assert client.post("/auth/logout").status_code == 204
    same_owner_login = client.post(
        "/auth/login",
        json={
            "email": "g70-recovery-other-owner@example.com",
            "password": same_tenant_owner_password,
        },
    )
    assert same_owner_login.status_code == 200, same_owner_login.text
    same_owner_response = client.post(
        url,
        json={"reason": "Another owner in the same tenant cannot recover this task"},
    )
    assert same_owner_response.status_code == 403, same_owner_response.text
    assert same_owner_response.json()["detail"]["code"] == "OWNER_SELF_ONLY"

    assert client.post("/auth/logout").status_code == 204
    admin_login = client.post(
        "/auth/login",
        json={
            "email": "g70-recovery-admin-no-delegation@example.com",
            "password": admin_password,
        },
    )
    assert admin_login.status_code == 200, admin_login.text
    admin_response = client.post(
        url,
        json={"reason": "Admin has no explicit delegation for recovery"},
    )
    assert admin_response.status_code == 403, admin_response.text
    assert admin_response.json()["detail"]["code"] == "PERMISSION_DENIED"

    assert client.post("/auth/logout").status_code == 204
    foreign_login = client.post(
        "/auth/login",
        json={
            "email": "g70-recovery-foreign-owner@example.com",
            "password": foreign_password,
        },
    )
    assert foreign_login.status_code == 200, foreign_login.text
    foreign_response = client.post(
        url,
        json={"reason": "Foreign tenant Owner cannot recover another tenant task"},
    )
    assert foreign_response.status_code == 404, foreign_response.text

    with sessions() as db:
        stored = db.get(DBGenerationTask, task_id)
        attempt = db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task_id
            )
        ).scalar_one()
        tenant = db.execute(select(DBTenant).where(DBTenant.id == stored.tenant_id)).scalar_one()
        assets = db.execute(
            select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == project["id"]
            )
        ).scalar_one()
        recovery_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task_id,
                DBGenerationEvent.event_type == "ARTIFACT_RECOVERY_COMPLETED",
            )
        ).scalar_one()
        usage = list(db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task_id
            )
        ).scalars())
        assert stored.status == "FAILED"
        assert stored.error_code == "ARTIFACT_INGEST_FAILED"
        assert attempt.reconciliation_state == "HELD"
        assert stored.attempts == 1
        assert assets == 0
        assert recovery_events == 0
        assert tenant.used_storage_bytes == 0
        assert sum(1 for row in usage if row.kind == "RESERVED") == 1
        assert sum(1 for row in usage if row.kind in {"RELEASED", "SETTLED"}) == 0
    assert media_store.uploads == 0
    assert media_store.inspections == 0
    app.state.moneyprinter.assert_not_called()


def test_im15_01_new_environment_is_disabled_without_provider_calls(provider_control_context):
    client, sessions, app, _ = provider_control_context
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is False
    assert readiness["reasonCode"] == "OWNER_CONFIG_MISSING"
    assert not hasattr(app.state, "moneyprinter")
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0


@pytest.mark.parametrize("mode", [None, "", "MONEYPRINTER", "unknown"])
def test_im15_02_unknown_or_disguised_operator_mode_is_disabled(tmp_path, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("AETHER_GENERATION_PROVIDER_MODE", raising=False)
    else:
        monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", mode)
    engine = build_engine(f"sqlite:///{tmp_path / f'mode-{mode or "missing"}.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    app = create_app(
        app_engine=engine, db_dependency=lambda: iter(()),
        bootstrap_admin_password=OWNER_PASSWORD,
    )
    assert app.state.generation_provider_mode == "disabled"
    engine.dispose()


def test_im15_03_editor_and_viewer_cannot_mutate_provider_control(provider_control_context):
    client, _, _, _ = provider_control_context
    for role in ("editor", "viewer"):
        password = secrets.token_urlsafe(24)
        assert client.post("/admin/users", json={
            "email": f"{role}@example.com", "displayName": role,
            "password": password, "role": role,
        }).status_code == 201
        client.post("/auth/logout")
        client.post("/auth/login", json={"email": f"{role}@example.com", "password": password})
        assert client.post(
            "/generation/providers/moneyprinter/config-versions",
            json=provider_policy(),
        ).status_code == 403
        assert client.post(
            "/generation/providers/moneyprinter/kill-switch",
            json={"disabled": True, "reasonCode": "OWNER_STOP"},
        ).status_code == 403
        client.post("/auth/logout")
        client.post("/auth/login", json={"email": "provider-owner@example.com", "password": OWNER_PASSWORD})


def test_im15_04_owner_draft_requires_csrf_and_has_no_provider_call(provider_control_context):
    client, sessions, app, _ = provider_control_context
    del client.headers["X-Aether-CSRF"]
    assert client.post(
        "/generation/providers/moneyprinter/config-versions", json=provider_policy()
    ).status_code == 403
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationProviderConfigVersion.id))).scalar_one() == 0
    assert not hasattr(app.state, "moneyprinter")


@pytest.mark.parametrize(
    "unsafe",
    [
        {"providerUrl": "https://evil.example"},
        {"apiKey": "value"},
        {"description": "authorization token"},
    ],
)
def test_im15_05_unknown_url_and_secret_shaped_policy_is_rejected(provider_control_context, unsafe):
    client, sessions, _, _ = provider_control_context
    payload = provider_policy()
    payload.update(unsafe)
    assert client.post(
        "/generation/providers/moneyprinter/config-versions", json=payload
    ).status_code == 422
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationProviderConfigVersion.id))).scalar_one() == 0


def test_im15_06_published_config_is_immutable_and_new_version_appends(provider_control_context):
    client, _, _, _ = provider_control_context
    first = publish_provider_config(client, create_provider_config(client))
    assert client.post(
        f"/generation/providers/moneyprinter/config-versions/{first['id']}/publish"
    ).status_code == 409
    second = create_provider_config(client, configLabel="provider-readiness-v2")
    assert second["version"] == 2
    assert second["supersedesId"] == first["id"]
    publish_provider_config(client, second)
    versions = client.get("/generation/providers/moneyprinter/config-versions").json()
    assert [item["status"] for item in versions] == ["PUBLISHED", "SUPERSEDED"]


def test_im15_07_owner_publish_cannot_override_operator_disabled(provider_control_context):
    client, _, app, _ = provider_control_context
    publish_provider_config(client, create_provider_config(client))
    app.state.generation_provider_mode = "disabled"
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is False
    assert readiness["reasonCode"] == "OPERATOR_DISABLED"


def test_im15_08_operator_mode_without_published_config_is_disabled(provider_control_context):
    client, _, app, _ = provider_control_context
    assert app.state.generation_provider_mode == "moneyprinter"
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is False
    assert readiness["ownerPolicy"]["published"] is False


def test_im15_09_published_config_without_worker_proof_is_disabled(provider_control_context):
    client, _, _, _ = provider_control_context
    publish_provider_config(client, create_provider_config(client))
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["reasonCode"] == "WORKER_ATTESTATION_MISSING"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("policyHash", "b" * 64, "WORKER_POLICY_MISMATCH"),
        ("upstreamPin", "wrong-pin", "WORKER_UPSTREAM_PIN_MISMATCH"),
        ("operatorMode", "disabled", "WORKER_OPERATOR_MODE_MISMATCH"),
    ],
)
def test_im15_10_mismatched_worker_proof_disables_readiness(provider_control_context, field, value, expected):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config, **{field: value}).status_code == 201
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is False
    assert readiness["reasonCode"] == expected


def test_im15_11_bad_worker_token_has_zero_state_change(provider_control_context):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
    now = datetime.datetime.now(datetime.timezone.utc)
    response = client.post(
        "/internal/generation/providers/moneyprinter/attest",
        headers={"X-Worker-Token": "wrong", "X-Worker-Id": "attacker"},
        json={
            "provider": "moneyprinter", "operatorMode": "moneyprinter",
            "tenantId": tenant_id, "configVersionId": config["id"],
            "policyHash": config["policyHash"], "adapterVersion": "v",
            "upstreamPin": "wrong", "healthy": True, "capabilities": {},
            "credentialState": "PRESENT", "networkIsolation": "ENFORCED",
            "canaryProfile": "private-one-task-v1",
            "checkedAt": now.isoformat(),
            "expiresAt": (now + datetime.timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 401
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationProviderAttestation.id))).scalar_one() == 0


def test_im15_12_matching_fresh_proof_produces_stable_capability_hash(provider_control_context):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    attested = attest_provider(client, sessions, config)
    assert attested.status_code == 201, attested.text
    first = client.get("/generation/providers/moneyprinter/readiness").json()
    second = client.get("/generation/providers/moneyprinter/readiness").json()
    assert first["enabled"] is True
    assert first["snapshotHash"] == second["snapshotHash"]
    assert len(first["snapshotHash"]) == 64


def test_im15_13_superseded_snapshot_hash_is_rejected_without_task(provider_control_context):
    client, sessions, _, _ = provider_control_context
    first = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, first).status_code == 201
    project = create_project(client)
    stale = generation_request(client, project)
    second = publish_provider_config(
        client, create_provider_config(client, configLabel="provider-readiness-v2")
    )
    assert second["id"] != first["id"]
    response = client.post(f"/projects/{project['id']}/generation-tasks", json=stale)
    assert response.status_code in {409, 503}
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0


def test_im15_14_readiness_is_sanitized_and_legacy_probes_are_gone(provider_control_context):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config).status_code == 201
    serialized = str(client.get("/generation/providers/moneyprinter/readiness").json()).lower()
    for forbidden in ("http://", "https://", "api_key", "authorization", "cookie", "localhost"):
        assert forbidden not in serialized
    assert client.get("/moneyprinter/health").status_code == 410
    assert client.get("/moneyprinter/capabilities").status_code == 410


def test_im15_15_provider_control_recovers_from_persistent_state(provider_control_context):
    client, sessions, _, engine = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config).status_code == 201

    def get_db():
        with sessions() as db:
            yield db

    restarted = create_app(
        app_engine=engine, db_dependency=get_db,
        video_use_adapter=FakeGeneratedMediaStore(),
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="provider-owner@example.com",
        worker_token=WORKER_TOKEN, cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="moneyprinter",
    )
    with TestClient(restarted) as restarted_client:
        restarted_client.post("/auth/login", json={
            "email": "provider-owner@example.com", "password": OWNER_PASSWORD,
        })
        readiness = restarted_client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is True
    assert readiness["configVersionId"] == config["id"]


def test_im15_16_compose_and_environment_templates_default_disabled():
    root = Path(__file__).resolve().parents[2]
    for path in (root / ".env.example", root / "infra/docker/.env.example"):
        assert "AETHER_GENERATION_PROVIDER_MODE=disabled" in path.read_text()
    compose = (root / "infra/docker/docker-compose.yml").read_text()
    assert compose.count("AETHER_GENERATION_PROVIDER_MODE=${AETHER_GENERATION_PROVIDER_MODE:-disabled}") == 2


def test_im18_10_readiness_exposes_only_sanitized_canary_proof(provider_control_context):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config).status_code == 201
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is True
    assert readiness["credentialState"] == "PRESENT"
    assert readiness["networkIsolation"] == "ENFORCED"
    assert readiness["canaryProfile"] == "private-one-task-v1"
    serialized = json.dumps(readiness).lower()
    for forbidden in ("configpath", "mtime", "configsha256", "api_key", "cookie"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credentialState", "INVALID"),
        ("networkIsolation", "NOT_ENFORCED"),
        ("canaryProfile", "disabled"),
    ],
)
def test_im18_13_healthy_attestation_rejects_incomplete_canary_proof(
    provider_control_context, field, value
):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    response = attest_provider(client, sessions, config, **{field: value})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "WORKER_CANARY_PROOF_INVALID"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"artifactPathPrefixes": ["/artifacts/"]}, "CANARY_POLICY_MISMATCH"),
        ({"concurrentTaskLimit": 2}, "CANARY_POLICY_MISMATCH"),
        ({"monthlyRequestLimit": 2}, "CANARY_POLICY_MISMATCH"),
        ({"monthlyGeneratedSecondsLimit": 11}, "CANARY_POLICY_MISMATCH"),
        ({"maxOutputs": 2}, "CANARY_POLICY_MISMATCH"),
    ],
)
def test_im20_30_non_canary_owner_policy_fails_closed(
    provider_control_context, overrides, expected
):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client, **overrides))
    assert attest_provider(client, sessions, config).status_code == 201
    readiness = client.get("/generation/providers/moneyprinter/readiness").json()
    assert readiness["enabled"] is False
    assert readiness["reasonCode"] == expected


def test_im20_33_canary_policy_allows_only_one_reserved_request(provider_control_context):
    client, sessions, _, _ = provider_control_context
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config).status_code == 201
    project = create_project(client, "Private canary")
    first_key = "private-canary-first"
    first = client.post(
        f"/projects/{project['id']}/generation-tasks",
        headers={"Idempotency-Key": idempotency_uuid(first_key)},
        json=generation_request(client, project, idempotency_key=first_key),
    )
    assert first.status_code == 202
    second_key = "private-canary-second"
    second = client.post(
        f"/projects/{project['id']}/generation-tasks",
        headers={"Idempotency-Key": idempotency_uuid(second_key)},
        json=generation_request(client, project, idempotency_key=second_key),
    )
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "GENERATION_CONCURRENCY_QUOTA_EXCEEDED"
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 1


def test_im17_33_validate_reports_quota_without_reservation(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/validate",
        json=generation_request(client, project),
    )
    assert response.status_code == 200
    assert response.json()["quota"]["concurrentRemaining"] > 0
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 0


def test_im17_34_create_atomically_writes_one_reserved_entry(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    with sessions() as db:
        entries = list(db.execute(select(DBGenerationUsageEntry)).scalars())
    assert len(entries) == 1
    assert entries[0].kind == "RESERVED" and entries[0].task_id == task["taskId"]


def test_im17_35_concurrent_create_never_exceeds_limit(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    responses = []
    for index in range(5):
        key = f"concurrency-{index}"
        responses.append(client.post(
            f"/projects/{project['id']}/generation-tasks",
            headers={"Idempotency-Key": idempotency_uuid(key)},
            json=generation_request(client, project, idempotency_key=key),
        ))
    assert [response.status_code for response in responses].count(202) == 4
    assert responses[-1].json()["detail"]["code"] == "GENERATION_CONCURRENCY_QUOTA_EXCEEDED"
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 4


def test_im17_36_monthly_request_and_seconds_limits_reject_new_work(generation_context, monkeypatch):
    from app import generation_tasks as generation_domain

    monkeypatch.setitem(generation_domain.DEFAULT_FAKE_POLICY, "monthlyRequestLimit", 1)
    client, sessions, _, _ = generation_context
    project = create_project(client)
    create_task(client, project, key="monthly-one")
    with sessions() as db:
        row = db.execute(select(DBGenerationTask)).scalar_one()
        row.status = "FAILED"
        db.commit()
    response = client.post(
        f"/projects/{project['id']}/generation-tasks",
        json=generation_request(client, project, idempotency_key="monthly-two"),
    )
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "GENERATION_MONTHLY_REQUEST_QUOTA_EXCEEDED"


def test_im17_37_cancel_releases_once_without_negative_usage(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    path = f"/projects/{project['id']}/generation-tasks/{task['taskId']}/cancel"
    assert client.post(path).status_code == 200
    assert client.post(path).status_code == 200
    with sessions() as db:
        entries = list(db.execute(select(DBGenerationUsageEntry)).scalars())
    assert [entry.kind for entry in entries] == ["RESERVED", "RELEASED"]
    assert sum(entry.request_units if entry.kind == "RESERVED" else -entry.request_units for entry in entries) == 0


def test_im17_38_success_settles_probe_seconds_once(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = ingest_success(client, project)
    client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact-1"},
        files={"file": ("artifact.mp4", b"deterministic-video", "video/mp4")},
    )
    with sessions() as db:
        settled = list(db.execute(select(DBGenerationUsageEntry).where(DBGenerationUsageEntry.kind == "SETTLED")).scalars())
    assert len(settled) == 1 and settled[0].generated_seconds == 1


def test_im17_39_retry_reuses_reservation_and_appends_attempt(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project)
    claim(client)
    transition(client, task["taskId"], "FAILED", errorCode="TEMPORARY", retryable=True)
    assert client.post(f"/projects/{project['id']}/generation-tasks/{task['taskId']}/retry").status_code == 202
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one() == 2


def test_im17_40_multiple_worker_failures_open_circuit_once(generation_context, monkeypatch):
    from app import generation_tasks as generation_domain

    monkeypatch.setitem(generation_domain.DEFAULT_FAKE_POLICY, "failureThreshold", 1)
    client, sessions, _, _ = generation_context
    project = create_project(client)
    first = create_task(client, project, key="circuit-first")
    second = create_task(client, project, key="circuit-second")
    assert claim(client, "worker-a").json()["taskId"] == first["taskId"]
    assert claim(client, "worker-b").json()["taskId"] == second["taskId"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda values: transition(
                client, values[0], "FAILED", worker_id=values[1],
                errorCode="PROVIDER_5XX", retryable=True,
            ).status_code,
            [(first["taskId"], "worker-a"), (second["taskId"], "worker-b")],
        ))
    assert results == [200, 200]
    with sessions() as db:
        opened = db.execute(select(func.count(DBGenerationProviderEvent.id)).where(
            DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_OPENED"
        )).scalar_one()
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
    assert opened == 1 and circuit.state == "OPEN"


def test_im17_41_open_blocks_claim_and_half_open_allows_one_probe(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    create_task(client, project, key="half-open-a")
    create_task(client, project, key="half-open-b")
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
        db.add(DBGenerationCircuitState(
            id=str(uuid.uuid4()), tenant_id=tenant_id, provider="moneyprinter",
            state="OPEN", failure_timestamps_json=[], opened_at=now,
            cooldown_until=now + datetime.timedelta(minutes=1), updated_at=now,
        ))
        db.commit()
    assert claim(client).status_code == 204
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        circuit.cooldown_until = now - datetime.timedelta(seconds=1)
        db.commit()
    assert claim(client, "worker-a").status_code == 200
    assert claim(client, "worker-b").status_code == 204


def test_im17_42_half_open_success_closes_persistent_circuit(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = create_task(client, project, key="half-open-success")
    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
        db.add(DBGenerationCircuitState(
            id=str(uuid.uuid4()), tenant_id=tenant_id, provider="moneyprinter",
            state="OPEN", failure_timestamps_json=[now.isoformat()], opened_at=now,
            cooldown_until=now - datetime.timedelta(seconds=1), updated_at=now,
        ))
        db.commit()
    assert claim(client, "worker-a").status_code == 200
    transition(client, task["taskId"], "RUNNING", upstreamJobId="upstream")
    transition(client, task["taskId"], "INGESTING", upstreamJobId="upstream", providerArtifactId="artifact")
    assert client.post(
        f"/internal/generation-tasks/{task['taskId']}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "artifact"},
        files={"file": ("artifact.mp4", b"video", "video/mp4")},
    ).status_code == 201
    with sessions() as db:
        assert db.execute(select(DBGenerationCircuitState.state)).scalar_one() == "CLOSED"


def test_im17_43_owner_emergency_stop_blocks_validate_create_and_claim(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    create_task(client, project, key="kill-existing")
    response = client.post(
        "/generation/providers/moneyprinter/kill-switch",
        json={"disabled": True, "reasonCode": "OWNER_EMERGENCY"},
    )
    assert response.status_code == 200 and response.json()["killSwitch"]["disabled"] is True
    request = generation_request(client, project, idempotency_key="kill-new")
    assert client.post(f"/projects/{project['id']}/generation-tasks/validate", json=request).status_code == 503
    assert claim(client).status_code == 204
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationProviderEvent.id)).where(
            DBGenerationProviderEvent.event_type == "PROVIDER_EMERGENCY_STOPPED"
        )).scalar_one() == 1


def test_im17_44_stop_and_recovery_preserve_governance_evidence(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client)
    task = ingest_success(client, project)
    with sessions() as db:
        before = {
            "assets": db.execute(select(func.count(DBAssetVersion.id))).scalar_one(),
            "attempts": db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one(),
            "events": db.execute(select(func.count(DBGenerationEvent.id))).scalar_one(),
            "usage": db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one(),
        }
    client.post("/generation/providers/moneyprinter/kill-switch", json={"disabled": True, "reasonCode": "OWNER_STOP"})
    client.post("/generation/providers/moneyprinter/kill-switch", json={"disabled": False, "reasonCode": "OWNER_RECOVERY"})
    with sessions() as db:
        after = {
            "assets": db.execute(select(func.count(DBAssetVersion.id))).scalar_one(),
            "attempts": db.execute(select(func.count(DBGenerationAttempt.id))).scalar_one(),
            "events": db.execute(select(func.count(DBGenerationEvent.id))).scalar_one(),
            "usage": db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one(),
        }
        provider_events = db.execute(select(func.count(DBGenerationProviderEvent.id))).scalar_one()
    assert after == before
    assert provider_events >= 2
    assert task["storedStatus"] == "RIGHTS_BLOCKED"


def test_im17_47_test_runtime_is_fake_only_and_real_mode_defaults_disabled():
    source = inspect.getsource(create_app)
    app = create_app(generation_provider_mode="unknown")
    assert app.state.generation_provider_mode == "disabled"
    assert "AETHER_GENERATION_PROVIDER_MODE" in source
    assert "deterministic-fake" in source


def test_im17_48_static_scope_has_no_dependency_pin_or_public_provider_egress():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "infra/docker/docker-compose.yml").read_text()
    assert "475f21147f0808f5ffe3f58af9ab794b28a4da2c" in compose
    api_source = (root / "apps/api/app/main.py").read_text()
    assert "MONEYPRINTER_API_URL" not in api_source
    assert "MoneyPrinterTurboAdapter" not in api_source


def test_p0_runtime_bridge_requires_rights_and_explicit_action(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client, "P0 bridge rights")
    task = ingest_success(client, project, key="p0-bridge-rights")
    current = client.get(f"/projects/{project['id']}").json()
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/{task['taskId']}/apply-talking-head-draft",
        json={"expectedRevision": current["revision"], "aspect": "9:16", "subtitles": []},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "RIGHTS_MISSING"
    unchanged = client.get(f"/projects/{project['id']}").json()
    assert unchanged["revision"] == current["revision"]
    assert unchanged["timeline"]["tracks"] == []


def test_p0_runtime_bridge_blocks_self_asserted_rights_without_trusted_issuer(generation_context):
    client, sessions, _, _ = generation_context
    project = create_project(client, "P0 bridge apply")
    task = ingest_success(client, project, key="p0-bridge-apply")
    result = task["results"][0]
    rights = client.post(
        f"/projects/{project['id']}/asset-versions/{result['assetVersionId']}/rights-snapshots",
        json={
            "status": "ALLOWED",
            "purpose": "EXPORT",
            "territory": "GLOBAL",
            "evidenceRef": "owner-self-asserted-p0-runtime-bridge",
        },
    )
    assert rights.status_code == 201
    current = client.get(f"/projects/{project['id']}").json()
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/{task['taskId']}/apply-talking-head-draft",
        json={
            "expectedRevision": current["revision"],
            "aspect": "9:16",
            "subtitles": [{"text": "口播测试", "startMs": 0, "durationMs": 1000}],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "RIGHTS_TRUSTED_ISSUER_REQUIRED"
    unchanged = client.get(f"/projects/{project['id']}").json()
    assert unchanged["revision"] == current["revision"]
    assert unchanged["timeline"]["tracks"] == []
    with sessions() as db:
        assert db.execute(select(func.count(DBCandidate.id))).scalar_one() == 0


def test_p0_runtime_bridge_revision_conflict_is_fail_closed(generation_context):
    client, _, _, _ = generation_context
    project = create_project(client, "P0 bridge revision")
    task = ingest_success(client, project, key="p0-bridge-revision")
    result = task["results"][0]
    assert client.post(
        f"/projects/{project['id']}/asset-versions/{result['assetVersionId']}/rights-snapshots",
        json={"status": "ALLOWED", "purpose": "EXPORT", "territory": "GLOBAL"},
    ).status_code == 201
    current = client.get(f"/projects/{project['id']}").json()
    response = client.post(
        f"/projects/{project['id']}/generation-tasks/{task['taskId']}/apply-talking-head-draft",
        json={"expectedRevision": current["revision"] - 1, "aspect": "9:16", "subtitles": []},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONCURRENCY_CONFLICT"



def _g72_fake_visual_fixture(sessions, project, allowed=True):
    """Native SQLite image + REMIX rights fixture; never production evidence."""
    with sessions() as db:
        record = db.get(DBProject, project["id"])
        assert record is not None
        created = datetime.datetime.now(datetime.timezone.utc)
        asset = DBAssetVersion(
            id=str(uuid.uuid4()), tenant_id=record.tenant_id, project_id=record.id,
            media_id="synthetic-fixture-image", version_no=1,
            sha256="a" * 64, media_type="image", content_type="image/png",
            size_bytes=100, probe_json={"image": {"width": 48, "height": 48}},
            created_by=record.owner_id, created_at=created,
        )
        db.add(asset)
        snapshot = DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=record.tenant_id,
            project_id=record.id, asset_version_id=asset.id,
            status="ALLOWED" if allowed else "DENIED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="synthetic-test-evidence", captured_by=record.owner_id,
            captured_at=created,
        )
        db.add(snapshot)
        db.commit()
        return asset.id


def _g72_fake_payload(client, project, asset_id, key="g72-fake-task"):
    caps = client.get("/generation/providers/moneyprinter/capabilities").json()
    return {
        "text": "synthetic offline voice-over",
        "durationSeconds": 1,
        "aspectRatio": "9:16",
        "resolution": "720p",
        "voiceId": "synthetic-seed",
        "imageAssetVersionId": asset_id,
        "idempotencyKey": idempotency_uuid(key),
        "expectedProjectRevision": project["revision"],
        "capabilitySnapshotHash": caps["snapshotHash"],
        "confirmSyntheticTestOnly": True,
    }


def test_g72_native_fake_http_db_create_claim_and_idempotency(generation_context):
    client, sessions, _app, _ = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id)
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    stale_snapshot_codes = {"CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED"}

    def refresh_preflight():
        last_response = None
        for _ in range(3):
            capabilities = client.get("/generation/providers/moneyprinter/capabilities")
            assert capabilities.status_code == 200, capabilities.text
            payload["capabilitySnapshotHash"] = capabilities.json()["snapshotHash"]
            last_response = client.post(path + "/validate", json=payload)
            if last_response.status_code == 200:
                payload["capabilitySnapshotHash"] = last_response.json()["capabilitySnapshotHash"]
                return last_response
            code = last_response.json().get("detail", {}).get("code")
            if code not in stale_snapshot_codes:
                return last_response
        return last_response

    preflight = refresh_preflight()
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["simulatedOutput"] is True

    created = None
    for _ in range(3):
        created = client.post(path, json=payload)
        if created.status_code == 202:
            break
        code = created.json().get("detail", {}).get("code")
        if created.status_code != 409 or code not in stale_snapshot_codes:
            break
        preflight = refresh_preflight()
        if preflight.status_code != 200:
            break
    assert created is not None and created.status_code == 202, created.text if created is not None else "create was not attempted"
    task = created.json()
    assert task["provider"] == "deterministic-fake-talking-head"
    assert task["status"] == "QUEUED"
    duplicate = client.post(path, json=payload)
    assert duplicate.status_code == 202, duplicate.text
    assert duplicate.json()["taskId"] == task["taskId"]
    claimed = claim(client)
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["providerMode"] == "talking-head-fake"
    assert body["taskId"] == task["taskId"]
    assert body["request"]["imageAssetId"] == asset_id
    assert body["request"]["fakeTaskType"] == "talking-head"
    with sessions() as db:
        stored = db.get(DBGenerationTask, task["taskId"])
        assert stored is not None and stored.requested_by is not None
        assert stored.max_attempts == 1
        assert len(list(db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == stored.id
        )).scalars())) == 1


def test_g72_native_fake_missing_asset_or_missing_rights_fail_closed(generation_context):
    client, sessions, _app, _ = generation_context
    project = create_project(client)
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    unknown = _g72_fake_payload(client, project, "nonexistent-asset", key="g72-bad-asset")
    response = client.post(path, json=unknown)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "ASSET_VERSION_NOT_FOUND"
    denied_id = _g72_fake_visual_fixture(sessions, project, allowed=False)
    denied = _g72_fake_payload(client, project, denied_id, key="g72-no-rights")
    response = client.post(path, json=denied)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FAKE_VISUAL_RIGHTS_BLOCKED"
    assert claim(client).status_code == 204


def test_g72_native_fake_owner_session_only_and_default_off(generation_context):
    client, sessions, app, _ = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    payload = _g72_fake_payload(client, project, asset_id, key="g72-overrides")
    malicious = {**payload, "requestedBy": "another-user", "tenantId": "another-tenant"}
    assert client.post(path, json=malicious).status_code == 422
    assert client.post(path, json={**payload, "confirmSyntheticTestOnly": False}).status_code == 422
    app.state.generation_provider_mode = "disabled"
    response = client.post(path, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "TALKING_HEAD_FAKE_DISABLED"
    assert claim(client).status_code == 204


def test_g72_native_fake_rights_revoke_after_enqueue_blocks_claim(generation_context):
    client, sessions, _app, _ = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-revoke-before-claim")
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    created = client.post(path, json=payload)
    assert created.status_code == 202, created.text
    with sessions() as db:
        old = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == asset_id
        )).scalar_one()
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=old.tenant_id, project_id=old.project_id,
            asset_version_id=asset_id, status="DENIED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="test-revoked", captured_by=old.captured_by,
            captured_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1),
        ))
        db.commit()
    # Revocation before claim must fail closed without consuming the task,
    # creating an attempt, releasing the reservation, or settling usage.
    with sessions() as db:
        before_task = db.get(DBGenerationTask, created.json()["taskId"])
        assert before_task is not None
        before_state = (
            before_task.status,
            before_task.error_code,
            before_task.asset_version_id,
            before_task.attempts,
        )
        before_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == before_task.id
        )).scalar_one()
        before_usage = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == before_task.id
        )).scalar_one()
        before_reserved = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == before_task.id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one()
        before_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == before_task.id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()

    assert claim(client).status_code == 204

    with sessions() as db:
        after_task = db.get(DBGenerationTask, created.json()["taskId"])
        assert after_task is not None
        after_state = (
            after_task.status,
            after_task.error_code,
            after_task.asset_version_id,
            after_task.attempts,
        )
        after_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == after_task.id
        )).scalar_one()
        after_usage = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == after_task.id
        )).scalar_one()
        after_reserved = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == after_task.id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one()
        after_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == after_task.id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()

    assert before_state == after_state
    assert after_state[0] == "QUEUED"
    assert before_events == after_events
    assert before_usage == after_usage
    assert before_reserved == after_reserved == 1
    assert before_settled == after_settled == 0


def test_g72_native_fake_revoked_input_rejected_at_artifact_intake(generation_context):
    client, sessions, _app, media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-revoke-at-intake")
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    response = client.post(path, json=payload)
    assert response.status_code == 202
    task_id = response.json()["taskId"]
    assert claim(client).status_code == 200
    with sessions() as db:
        old = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == asset_id
        )).scalar_one()
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=old.tenant_id, project_id=old.project_id,
            asset_version_id=asset_id, status="REVOKED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="test-revoked", captured_by=old.captured_by,
            captured_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1),
        ))
        db.commit()
    response = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "synthetic-job-id"},
        files={"file": ("synthetic.mp4", b"synthetic-mp4-bytes", "video/mp4")},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"
    assert media.uploads == 0


@pytest.fixture()
def g72_true_native_flow_context(tmp_path, monkeypatch):
    """FAKE-only HTTP+SQLite+native Worker with local FFmpeg; mock media store only."""
    monkeypatch.setenv("AETHER_MAX_UPLOAD_BYTES", str(16 * 1024 * 1024))
    engine = build_engine(f"sqlite:///{tmp_path / 'g72-real-worker.db'}")
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        with sessions() as db:
            yield db

    media_store = FakeGeneratedMediaStore()
    app = create_app(
        app_engine=engine, db_dependency=get_db, video_use_adapter=media_store,
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    with LeaseAwareTestClient(app) as client:
        assert client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": OWNER_PASSWORD},
        ).status_code == 200
        client.headers.update({"X-Aether-CSRF": "1"})
        yield client, sessions, media_store
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_g72_native_worker_fastapi_sqlite_fake_flow(
    g72_true_native_flow_context, monkeypatch,
):
    """Execute native API -> SQLite -> Worker -> offline mock-store intake.

    Only where FFmpeg exists does the native fake encoder render MP4.
    CI lint runners lack FFmpeg, and replace ONLY the encoder's media bytes.
    Neither path substitutes for full Compose/PostgreSQL/video-use evidence.
    """
    import importlib
    import importlib.util
    import shutil
    import sys

    # Hosted CI lint runners do not include FFmpeg; the Docker Worker image does.
    # In this runner we still exercise real API, SQLite and Worker dispatch;
    # only the local encoder's binary media output is substituted by a fixture.
    has_ffmpeg = shutil.which("ffmpeg") is not None
    client, sessions, media_store = g72_true_native_flow_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-native-full-fake-flow")
    base_path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    create = client.post(base_path, json=payload)
    assert create.status_code == 202, create.text
    task_id = create.json()["taskId"]

    # Load the actual Worker package under a separate package name. The API
    # already owns the 'app' module name in this test Python process.
    worker_pkg_dir = Path(__file__).resolve().parents[1] / "worker" / "app"
    package_name = "_aether_g72_native_worker"
    package_spec = importlib.util.spec_from_file_location(
        package_name, worker_pkg_dir / "__init__.py",
        submodule_search_locations=[str(worker_pkg_dir)],
    )
    assert package_spec is not None and package_spec.loader is not None
    package = importlib.util.module_from_spec(package_spec)
    sys.modules[package_name] = package
    package_spec.loader.exec_module(package)
    worker = importlib.import_module(f"{package_name}.main")
    fake_module = importlib.import_module(f"{package_name}.deterministic_fake_adapter")
    fake_bridge = importlib.import_module(
        f"{package_name}.deterministic_fake_talking_head_adapter"
    )
    # Keep the real encoder fast. Synthesized output remains a true MP4 with
    # H.264/AAC, but its visual size is reduced in this test only.
    monkeypatch.setitem(fake_module._ASPECT_DIMENSIONS, "9:16", (320, 568))
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", "deterministic-fake")

    class NativeApiTestQueue:
        """HTTP-equivalent TestClient transport; no external network or credentials."""
        def __init__(self, test_client):
            self.test_client = test_client
            self.worker_headers = {
                "X-Worker-Token": WORKER_TOKEN,
                "X-Worker-Id": "worker-g72-native",
            }

        def claim(self):
            response = self.test_client.post(
                "/internal/generation-tasks/claim",
                headers=self.worker_headers,
            )
            assert response.status_code in {200, 204}, response.text
            return response.json() if response.status_code == 200 else None

        def heartbeat(self, task_id):
            response = self.test_client.post(
                f"/internal/generation-tasks/{task_id}/heartbeat",
                headers=self.worker_headers, json={},
            )
            assert response.status_code == 200, response.text
            return response.json()

        def transition(self, task_id, **values):
            data = {
                "status": values.get("status"),
                "progress": values.get("progress"),
                "message": values.get("message"),
                "upstreamJobId": values.get("upstream_job_id"),
                "providerArtifactId": values.get("provider_artifact_id"),
                "errorCode": values.get("error_code"),
                "errorMessage": values.get("error_message"),
                "retryable": values.get("retryable", False),
            }
            response = self.test_client.post(
                f"/internal/generation-tasks/{task_id}/transition",
                headers=self.worker_headers, json=data,
            )
            assert response.status_code == 200, response.text
            return response.json()

        def artifact_intake(self, task_id, provider_artifact_id, stream):
            response = self.test_client.post(
                f"/internal/generation-tasks/{task_id}/artifact-intake",
                headers=self.worker_headers,
                data={"providerArtifactId": provider_artifact_id},
                files={"file": ("synthetic.mp4", stream, "video/mp4")},
            )
            assert response.status_code == 201, response.text
            return response.json()

    queue = NativeApiTestQueue(client)
    claimed = queue.claim()
    assert claimed is not None and claimed["taskId"] == task_id
    assert claimed["providerMode"] == "talking-head-fake"
    local_native = fake_module.DeterministicFakeAdapter()
    if not has_ffmpeg:
        monkeypatch.setattr(
            local_native, "_render",
            lambda _job: b"synthetic-fixture-not-real-media",
        )
    bridge = fake_bridge.DeterministicFakeTalkingHeadAdapter(local_native)
    runtime = worker.WorkerComponents(
        ffmpeg=None, ai=None, recovery=None, moneyprinter=None, video_use=None,
        generation_queue=queue, talking_head=None, talking_head_fake=bridge,
    )
    outcome = worker.process_generation_task(runtime, claimed, poll_interval=0)
    assert outcome["storedStatus"] == "RIGHTS_BLOCKED", outcome
    assert media_store.uploads == 1
    assert queue.claim() is None
    with sessions() as db:
        persisted = db.get(DBGenerationTask, task_id)
        assert persisted is not None
        assert persisted.status == "RIGHTS_BLOCKED"
        assert persisted.upstream_job_id is not None
        assert persisted.asset_version_id is not None
        output_asset = db.get(DBAssetVersion, persisted.asset_version_id)
        assert output_asset is not None
        assert output_asset.sha256 and len(output_asset.sha256) == 64
        assert output_asset.media_type == "video"
        assert output_asset.probe_json["provenance"]["generationTaskId"] == task_id
        events = list(db.execute(select(DBGenerationEvent).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
        )).scalars())
        assert len(events) == 1


def test_g72_viewer_rejected_without_generation_side_effects(generation_context):
    """Native HTTP role gate must reject viewer before writing FAKE reservations."""
    client, sessions, _app, _media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-viewer-role")
    password = secrets.token_urlsafe(24)
    registered = client.post("/admin/users", json={
        "email": "g72-viewer@example.com", "displayName": "G72 Viewer",
        "password": password, "role": "viewer",
    })
    assert registered.status_code == 201, registered.text
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={
        "email": "g72-viewer@example.com", "password": password,
    }).status_code == 200
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    for endpoint in (path + "/validate", path):
        rejected = client.post(endpoint, json=payload)
        assert rejected.status_code == 403, rejected.text
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 0


def test_g72_changed_payload_same_idempotency_key_is_rejected(generation_context):
    """The same authenticated owner cannot reuse a key with altered content."""
    client, sessions, _app, _media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-body-mutation")
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    first = client.post(path, json=payload)
    assert first.status_code == 202, first.text
    changed = {**payload, "text": "different FAKE-only narration, same request key"}
    second = client.post(path, json=changed)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with sessions() as db:
        tasks = list(db.execute(select(DBGenerationTask)).scalars())
        assert len(tasks) == 1 and tasks[0].id == first.json()["taskId"]
        assert len(list(db.execute(select(DBGenerationAttempt)).scalars())) == 1
        assert len(list(db.execute(select(DBGenerationUsageEntry)).scalars())) == 1


def test_g72_native_worker_ffmpeg_to_durable_local_file_and_audit(
    g72_true_native_flow_context, monkeypatch, tmp_path,
):
    """FAKE-only API -> SQLite -> Worker -> actual FFmpeg -> temporary disk -> DB audit.

    Deliberately NOT a video-use adapter / PostgreSQL / production E2E test.
    """
    import hashlib
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("Dedicated G72 offline integration requires FFmpeg and FFprobe")

    _client, sessions, media_store = g72_true_native_flow_context
    durable_dir = tmp_path / "g72-durable-media"
    durable_dir.mkdir()
    saved_files = []

    def offline_durable_upload(project_id, filename, content_type, stream):
        assert content_type == "video/mp4"
        assert filename.endswith(".mp4")
        media_id = f"g72-disk-{uuid.uuid4().hex}"
        path = durable_dir / f"{media_id}.mp4"
        path.write_bytes(stream.read())
        saved_files.append(path)
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height",
                "-of", "json", str(path),
            ],
            check=True, capture_output=True, text=True, timeout=20,
        )
        probed = json.loads(result.stdout)
        tracks = probed.get("streams") or []
        videos = [track for track in tracks if track.get("codec_type") == "video"]
        audios = [track for track in tracks if track.get("codec_type") == "audio"]
        assert len(videos) == 1 and videos[0].get("codec_name") == "h264"
        assert len(audios) == 1 and audios[0].get("codec_name") == "aac"
        assert int(videos[0].get("width") or 0) > 0
        assert int(videos[0].get("height") or 0) > 0
        duration = float((probed.get("format") or {}).get("duration") or 0)
        assert 0.5 <= duration <= 3.0
        media_store.uploads += 1
        return {
            "mediaId": media_id,
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": {
                "durationSeconds": duration,
                "sizeBytes": path.stat().st_size,
                "video": {
                    "codec": "h264",
                    "width": int(videos[0]["width"]),
                    "height": int(videos[0]["height"]),
                },
                "audio": {"codec": "aac"},
            },
        }

    # Only the test video-use replacement writes to local temporary storage;
    # the API, SQLite, Worker routing, FAKE adapter, FFmpeg and audit are native.
    monkeypatch.setattr(media_store, "upload_media", offline_durable_upload)
    test_g72_native_worker_fastapi_sqlite_fake_flow(
        g72_true_native_flow_context, monkeypatch,
    )
    assert len(saved_files) == 1
    raw = saved_files[0].read_bytes()
    assert len(raw) > 1024 and raw[4:8] == b"ftyp"
    with sessions() as db:
        task = db.execute(select(DBGenerationTask)).scalar_one()
        assert task.status == "RIGHTS_BLOCKED"
        assert task.media_id == saved_files[0].stem
        asset = db.get(DBAssetVersion, task.asset_version_id)
        assert asset is not None
        assert asset.sha256 == hashlib.sha256(raw).hexdigest()
        assert asset.size_bytes == len(raw)
        assert asset.probe_json["provenance"]["generationTaskId"] == task.id
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task.id,
            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
        )).scalar_one() == 1
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task.id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 1


def test_g72_native_video_use_http_store_survives_sidecar_restart(
    g72_true_native_flow_context, monkeypatch, tmp_path,
):
    """Native FAKE API/SQLite/Worker -> actual video-use HTTP -> disk -> restart.

    In a CI runner without FFmpeg/FFprobe ONLY encoder bytes and sidecar media
    probe are test doubles. API authorization, claim, Worker dispatch, HTTP
    upload, on-disk storage, output SHA and DB audit use native components.
    This is in-process nonproduction validation, not Compose/PG E2E.
    """
    import hashlib
    import importlib.util
    import shutil
    import sys

    _, sessions, media_store = g72_true_native_flow_context
    sidecar_path = Path(__file__).resolve().parents[1] / "video_use" / "app" / "main.py"
    module_name = f"_aether_g72_native_video_use_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, sidecar_path)
    assert spec is not None and spec.loader is not None
    sidecar_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = sidecar_module
    spec.loader.exec_module(sidecar_module)

    # The API-unit runner does not guarantee native encoders. Missing tooling
    # is explicitly modeled; the Docker media job validates genuine FFmpeg.
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        def fixture_probe(path):
            return {
                "durationSeconds": 1.0, "sizeBytes": path.stat().st_size,
                "video": {"codec": "h264", "width": 320, "height": 568},
                "audio": {"codec": "aac"},
            }
        monkeypatch.setattr(sidecar_module, "probe", fixture_probe)

    media_root = tmp_path / "g72-native-video-use-media"
    sidecar_app = sidecar_module.create_app(media_root=media_root)
    with TestClient(sidecar_app) as sidecar_client:
        def native_video_use_upload(project_id, filename, content_type, stream):
            response = sidecar_client.post(
                "/media", data={"projectId": project_id},
                files={"file": (filename, stream, content_type)},
            )
            assert response.status_code == 201, response.text
            media_store.uploads += 1
            return response.json()

        monkeypatch.setattr(media_store, "upload_media", native_video_use_upload)
        test_g72_native_worker_fastapi_sqlite_fake_flow(
            g72_true_native_flow_context, monkeypatch,
        )
        with sessions() as db:
            task = db.execute(select(DBGenerationTask)).scalar_one()
            assert task.status == "RIGHTS_BLOCKED"
            assert task.media_id and task.asset_version_id
            project_id, media_id = task.project_id, task.media_id
            asset = db.get(DBAssetVersion, task.asset_version_id)
            assert asset is not None
            sha256, size_bytes = asset.sha256, asset.size_bytes
            assert db.execute(select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id == task.id,
                DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
            )).scalar_one() == 1
        saved_file = media_root / "projects" / project_id / "media" / f"{media_id}.mp4"
        assert saved_file.is_file()
        payload = saved_file.read_bytes()
        assert payload and sha256 == hashlib.sha256(payload).hexdigest()
        assert size_bytes == len(payload)
        first_read = sidecar_client.get(f"/media/{project_id}/{media_id}")
        assert first_read.status_code == 200
        assert first_read.content == payload

    # Fresh service instance opens the existing persisted media directory.
    with TestClient(sidecar_module.create_app(media_root=media_root)) as restarted:
        response = restarted.get(f"/media/{project_id}/{media_id}")
        assert response.status_code == 200
        assert response.content == payload
        assert restarted.get(
            f"/media/{project_id}/{uuid.uuid4()}"
        ).status_code == 404


def test_g72_foreign_tenant_owner_cannot_preflight_create_or_read(generation_context):
    """Real auth/SQLite: even another tenant's owner cannot operate G72."""
    client, sessions, _app, media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-foreign-tenant-isolated")
    now = datetime.datetime.now(datetime.timezone.utc)
    password = secrets.token_urlsafe(24)
    with sessions() as db:
        tenant = DBTenant(
            id="g72-foreign-tenant", name="G72 Foreign", slug="g72-foreign",
            project_quota=50, storage_quota_bytes=1024**3,
            used_storage_bytes=0, concurrent_render_quota=2,
            monthly_render_seconds_quota=3600, render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"), created_at=now, updated_at=now,
        )
        user = DBUser(
            id="g72-foreign-owner", tenant_id=tenant.id,
            email="g72-foreign-owner@example.com", display_name="Other Tenant Owner",
            password_hash=hash_password(password), role="owner", is_active=True,
            created_at=now, updated_at=now,
        )
        db.add_all([tenant, user])
        db.commit()
    assert client.post("/auth/logout").status_code == 204
    assert client.post("/auth/login", json={
        "email": "g72-foreign-owner@example.com", "password": password,
    }).status_code == 200
    base = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    for endpoint in (base + "/validate", base):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 404, response.text
    assert client.get(f"/projects/{project['id']}").status_code == 404
    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id))).scalar_one() == 0
    assert media.uploads == 0


def test_g72_wrong_worker_cannot_ingest_claimed_artifact(generation_context):
    """Real FAKE API/SQLite lease guard: rejected intake has zero media side effect."""
    client, sessions, _app, media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-wrong-lease-owner")
    base = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    created = client.post(base, json=payload)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]
    assert claim(client, worker_id="worker-a").status_code == 200
    assert transition(client, task_id, "RUNNING", upstreamJobId="fake-job-a").status_code == 200
    assert transition(
        client, task_id, "INGESTING", progress=95,
        upstreamJobId="fake-job-a", providerArtifactId="fake-artifact-a",
    ).status_code == 200
    rejected = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-b"},
        data={"providerArtifactId": "fake-artifact-a"},
        files={"file": ("fake.mp4", b"synthetic-fake-media", "video/mp4")},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "LEASE_LOST"
    assert media.uploads == 0
    with sessions() as db:
        task = db.get(DBGenerationTask, task_id)
        assert task.status == "INGESTING" and task.asset_version_id is None
        assert db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id,
            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
        )).scalar_one() == 0
        assert db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one() == 0


# G76 safety regression: probe-only circuit closure (FAKE-only, native HTTP+SQLite).
# Keep the G76 gap OPEN until durable epoch/issuer and concurrency gates pass.
def _g76_finish_claimed_fake_task(client, task_id, worker_id, suffix):
    upstream = f"g76-upstream-{suffix}"
    artifact = f"g76-artifact-{suffix}"
    assert transition(
        client, task_id, "RUNNING", worker_id=worker_id, upstreamJobId=upstream
    ).status_code == 200
    assert transition(
        client, task_id, "INGESTING", worker_id=worker_id,
        upstreamJobId=upstream, providerArtifactId=artifact,
    ).status_code == 200
    result = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
        data={"providerArtifactId": artifact},
        files={"file": (f"{suffix}.mp4", b"g76-offline-test-video", "video/mp4")},
    )
    assert result.status_code == 201, result.text



def test_g76_stale_lease_epoch_cannot_mutate_reclaimed_half_open_probe(generation_context):
    """A stale completion from a recycled worker ID cannot impersonate the current probe."""
    client, sessions, _, _ = generation_context
    project = create_project(client, "G76 stale lease epoch")
    task = create_task(client, project, key="g76-stale-lease-epoch")
    worker_id = "worker-g76-reused"
    first = claim(client, worker_id).json()
    assert first["taskId"] == task["taskId"]
    stale_epoch = first["leaseEpoch"]

    failed = transition(
        client, task["taskId"], "FAILED", worker_id=worker_id,
        errorCode="PROVIDER_TEST_FAILURE", errorMessage="synthetic failure",
        retryable=True,
    )
    assert failed.status_code == 200, failed.text
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        circuit.state = "CLOSED"
        circuit.failure_timestamps_json = []
        circuit.opened_at = None
        circuit.cooldown_until = None
        circuit.half_open_task_id = None
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        db.commit()
    retried = client.post(f"/projects/{project['id']}/generation-tasks/{task['taskId']}/retry")
    assert retried.status_code == 202, retried.text

    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        circuit.state = "HALF_OPEN"
        circuit.failure_timestamps_json = [now.isoformat()]
        circuit.opened_at = now - datetime.timedelta(minutes=2)
        circuit.cooldown_until = now - datetime.timedelta(seconds=1)
        circuit.half_open_task_id = None
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        circuit.probe_epoch = 7
        circuit.updated_at = now
        db.commit()

    current = claim(client, worker_id).json()
    assert current["taskId"] == task["taskId"]
    current_epoch = current["leaseEpoch"]
    assert current_epoch > stale_epoch
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert circuit.state == "HALF_OPEN"
        assert circuit.half_open_task_id == task["taskId"]
        assert circuit.half_open_lease_epoch == current_epoch
        assert circuit.half_open_issuer == worker_id
        current_probe_epoch = circuit.probe_epoch
        assert current["circuitProbeEpoch"] == current_probe_epoch

    stale = client.post(
        f"/internal/generation-tasks/{task['taskId']}/transition",
        headers={
            "X-Worker-Token": WORKER_TOKEN,
            "X-Worker-Id": worker_id,
            "X-Worker-Lease-Epoch": str(stale_epoch),
        },
        json={
            "status": "RUNNING", "progress": 100, "message": "stale completion",
            "upstreamJobId": "g76-stale-upstream",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "LEASE_EPOCH_MISMATCH"
    with sessions() as db:
        current_task = db.execute(
            select(DBGenerationTask).where(DBGenerationTask.id == task["taskId"])
        ).scalar_one()
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert current_task.status == "SUBMITTING"
        assert circuit.state == "HALF_OPEN"
        assert circuit.probe_epoch == current_probe_epoch
        assert circuit.half_open_lease_epoch == current_epoch
        assert db.execute(
            select(func.count(DBGenerationProviderEvent.id)).where(
                DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED"
            )
        ).scalar_one() == 0

    _g76_finish_claimed_fake_task(client, task["taskId"], worker_id, "current-epoch")
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert circuit.state == "CLOSED"
        assert circuit.half_open_task_id is None
        assert circuit.half_open_lease_epoch is None
        assert circuit.half_open_issuer is None
        assert db.execute(
            select(func.count(DBGenerationProviderEvent.id)).where(
                DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED"
            )
        ).scalar_one() == 1


def test_g76_late_success_does_not_close_open_circuit(generation_context):
    """An older completion cannot heal OPEN or erase its cooldown."""
    client, sessions, _, _ = generation_context
    project = create_project(client, "G76 stale success during open")
    task = create_task(client, project, key="g76-late-success-open")
    assert claim(client, "worker-g76-old").json()["taskId"] == task["taskId"]

    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        circuit.state = "OPEN"
        circuit.failure_timestamps_json = [now.isoformat()]
        circuit.opened_at = now
        circuit.cooldown_until = now + datetime.timedelta(minutes=2)
        circuit.half_open_task_id = None
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        circuit.updated_at = now
        db.commit()

    _g76_finish_claimed_fake_task(client, task["taskId"], "worker-g76-old", "late-open")
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        closed_events = db.execute(
            select(func.count(DBGenerationProviderEvent.id)).where(
                DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED"
            )
        ).scalar_one()
        assert circuit.state == "OPEN"
        assert circuit.cooldown_until is not None
        assert circuit.half_open_task_id is None
        assert closed_events == 0


def test_g76_half_open_only_current_probe_can_close(generation_context):
    """Wrong task cannot close HALF_OPEN; current probe closes exactly once."""
    client, sessions, _, _ = generation_context
    project = create_project(client, "G76 wrong probe")
    old = create_task(client, project, key="g76-old-not-probe")
    probe = create_task(client, project, key="g76-current-probe")
    assert claim(client, "worker-g76-old").json()["taskId"] == old["taskId"]

    now = datetime.datetime.now(datetime.timezone.utc)
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        circuit.state = "HALF_OPEN"
        circuit.half_open_task_id = None
        circuit.probe_epoch = 0
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        circuit.failure_timestamps_json = [now.isoformat()]
        circuit.opened_at = now - datetime.timedelta(minutes=2)
        circuit.cooldown_until = None
        circuit.updated_at = now
        db.commit()
    assert claim(client, "worker-g76-probe").json()["taskId"] == probe["taskId"]

    _g76_finish_claimed_fake_task(client, old["taskId"], "worker-g76-old", "wrong-probe")
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert circuit.state == "HALF_OPEN"
        assert circuit.half_open_task_id == probe["taskId"]
        assert db.execute(
            select(func.count(DBGenerationProviderEvent.id)).where(
                DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED"
            )
        ).scalar_one() == 0

    _g76_finish_claimed_fake_task(client, probe["taskId"], "worker-g76-probe", "right-probe")
    with sessions() as db:
        circuit = db.execute(select(DBGenerationCircuitState)).scalar_one()
        assert circuit.state == "CLOSED"
        assert circuit.half_open_task_id is None
        assert db.execute(
            select(func.count(DBGenerationProviderEvent.id)).where(
                DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED"
            )
        ).scalar_one() == 1

@pytest.mark.parametrize(
    ("requested", "sandbox_opt_in", "runtime_stage", "expected"),
    [
        ("deterministic-fake", "true", "nonproduction", "deterministic-fake"),
        ("deterministic-fake", "false", "nonproduction", "disabled"),
        ("deterministic-fake", "true", "production", "disabled"),
        ("deterministic-fake", "true", "", "disabled"),
        ("disabled", "true", "nonproduction", "disabled"),
        ("moneyprinter", "false", "nonproduction", "moneyprinter"),
    ],
)
def test_g72_compose_fake_mode_requires_two_explicit_nonproduction_gates(
    monkeypatch, requested, sandbox_opt_in, runtime_stage, expected
):
    """A real API process must fail closed unless FAKE-only sandbox is explicit."""
    monkeypatch.setenv("AETHER_GENERATION_PROVIDER_MODE", requested)
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", sandbox_opt_in)
    if runtime_stage:
        monkeypatch.setenv("AETHER_RUNTIME_STAGE", runtime_stage)
    else:
        monkeypatch.delenv("AETHER_RUNTIME_STAGE", raising=False)
    application = create_app()
    assert application.state.generation_provider_mode == expected

@pytest.mark.parametrize("ineligible_count", [101, 205])
def test_g73_keyset_claim_scans_past_ineligible_queue_pages(
    generation_context, ineligible_count
):
    """Native API/SQLite FAKE-only: older, wrong-mode queue rows must not starve G72.

    All extra queued records are synthetic local DB fixtures.  They never
    contact an external Provider or consume a trusted billing source.
    """
    client, sessions, _app, media = generation_context
    project = create_project(client, "G73 keyset scan synthetic queue")
    asset_id = _g72_fake_visual_fixture(sessions, project)
    request = _g72_fake_payload(
        client, project, asset_id, key=f"g73-keyset-{ineligible_count}"
    )
    created = client.post(
        f"/projects/{project['id']}/talking-head/fake-generation-tasks",
        json=request,
    )
    assert created.status_code == 202, created.text
    eligible_id = created.json()["taskId"]

    with sessions() as db:
        project_row = db.get(DBProject, project["id"])
        eligible = db.get(DBGenerationTask, eligible_id)
        assert project_row is not None and eligible is not None
        first_created = eligible.created_at - datetime.timedelta(days=1)
        old_rows = []
        for idx in range(ineligible_count):
            created_at = first_created + datetime.timedelta(microseconds=idx)
            old_rows.append(DBGenerationTask(
                id=str(uuid.uuid4()), tenant_id=project_row.tenant_id,
                project_id=project_row.id, requested_by=project_row.owner_id,
                provider="moneyprinter", status="QUEUED", progress=0,
                message="synthetic older wrong-mode record",
                request_json={"syntheticFixtureOnly": True},
                request_hash=f"g73-fixture-{idx}",
                capability_snapshot_json={"mode": "moneyprinter"},
                capability_snapshot_hash="0" * 64,
                idempotency_key=f"g73-fixture-{ineligible_count}-{idx}",
                attempts=1, max_attempts=1,
                created_at=created_at, updated_at=created_at,
            ))
        db.add_all(old_rows)
        db.commit()

    claimed = claim(client, worker_id="g73-safe-fake-worker")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == eligible_id
    assert claimed.json()["providerMode"] == "talking-head-fake"
    with sessions() as db:
        eligible = db.get(DBGenerationTask, eligible_id)
        assert eligible.status == "SUBMITTING"
        assert eligible.lease_owner == "g73-safe-fake-worker"
        untouched = db.execute(select(func.count(DBGenerationTask.id)).where(
            DBGenerationTask.provider == "moneyprinter",
            DBGenerationTask.status == "QUEUED",
            DBGenerationTask.project_id == project["id"],
        )).scalar_one()
        assert untouched == ineligible_count
        claimed_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == eligible_id,
            DBGenerationEvent.event_type == "TASK_CLAIMED",
        )).scalar_one()
        assert claimed_events == 1
    assert media.uploads == 0


def test_g72_revocation_after_unknown_ack_preserves_unknown_and_reserved_usage(generation_context):
    """FAKE-only characterization: revoke after ambiguous ACK must not fabricate recovery."""
    client, sessions, _app, media = generation_context
    project = create_project(client)
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(client, project, asset_id, key="g72-revoke-after-unknown-ack")
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    created = client.post(path, json=payload)
    assert created.status_code == 202, created.text
    task_id = created.json()["taskId"]

    claimed = claim(client, worker_id="worker-a")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == task_id
    assert transition(
        client, task_id, "UNKNOWN", worker_id="worker-a",
        upstreamJobId="synthetic-upstream-ack",
        errorCode="ACK_UNCERTAIN",
        errorMessage="synthetic ambiguous acknowledgement",
    ).status_code == 200

    with sessions() as db:
        old = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == asset_id
        )).scalar_one()
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=old.tenant_id, project_id=old.project_id,
            asset_version_id=asset_id, status="REVOKED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="test-revoked-after-unknown-ack", captured_by=old.captured_by,
            captured_at=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1),
        ))
        db.commit()

    with sessions() as db:
        before_task = db.get(DBGenerationTask, task_id)
        assert before_task is not None
        before_state = (
            before_task.status, before_task.error_code, before_task.asset_version_id,
            before_task.media_id, before_task.attempts, before_task.upstream_job_id,
        )
        before_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id
        )).scalar_one()
        before_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project["id"]
        )).scalar_one()
        before_usage = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id
        )).scalar_one()
        before_reserved = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one()
        before_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        before_storage = db.execute(select(DBTenant.used_storage_bytes)).scalar_one()

    rejected = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "worker-a"},
        data={"providerArtifactId": "synthetic-late-artifact"},
        files={"file": ("synthetic.mp4", b"synthetic-late-artifact-bytes", "video/mp4")},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "FAKE_TALKING_HEAD_SOURCE_REVOKED"
    assert media.uploads == 0

    with sessions() as db:
        after_task = db.get(DBGenerationTask, task_id)
        assert after_task is not None
        after_state = (
            after_task.status, after_task.error_code, after_task.asset_version_id,
            after_task.media_id, after_task.attempts, after_task.upstream_job_id,
        )
        after_events = db.execute(select(func.count(DBGenerationEvent.id)).where(
            DBGenerationEvent.generation_task_id == task_id
        )).scalar_one()
        after_assets = db.execute(select(func.count(DBAssetVersion.id)).where(
            DBAssetVersion.project_id == project["id"]
        )).scalar_one()
        after_usage = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id
        )).scalar_one()
        after_reserved = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "RESERVED",
        )).scalar_one()
        after_settled = db.execute(select(func.count(DBGenerationUsageEntry.id)).where(
            DBGenerationUsageEntry.task_id == task_id,
            DBGenerationUsageEntry.kind == "SETTLED",
        )).scalar_one()
        after_storage = db.execute(select(DBTenant.used_storage_bytes)).scalar_one()

    assert before_state == after_state
    assert after_state[0] == "UNKNOWN"
    assert after_state[5] == "synthetic-upstream-ack"
    assert before_events == after_events
    assert before_assets == after_assets == 1  # source image remains; no generated asset was created
    assert before_usage == after_usage
    assert before_reserved == after_reserved == 1
    assert before_settled == after_settled == 0
    assert before_storage == after_storage


def test_g72_admin_without_explicit_delegation_cannot_create_owner_talking_head_task(generation_context):
    """No implicit admin-as-owner behavior: delegated operation needs an explicit grant."""
    client, sessions, _app, _media = generation_context
    project = create_project(client, "G72 admin delegation boundary")
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(
        client, project, asset_id, key="g72-admin-no-delegation"
    )
    path = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    password = secrets.token_urlsafe(24)
    now = datetime.datetime.now(datetime.timezone.utc)

    # Seed an admin identity directly: the Owner-only user-management route
    # must not be used to grant itself broader permissions.
    with sessions() as db:
        tenant_id = db.execute(select(DBTenant.id)).scalar_one()
        db.add(DBUser(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            email="g72-admin-no-delegation@example.com",
            display_name="G72 Admin Without Delegation",
            password_hash=hash_password(password),
            role="admin",
            is_active=True,
            created_at=now,
            updated_at=now,
        ))
        db.commit()
        before_tasks = db.execute(select(func.count(DBGenerationTask.id))).scalar_one()
        before_events = db.execute(select(func.count(DBGenerationEvent.id))).scalar_one()
        before_usage = db.execute(
            select(func.count(DBGenerationUsageEntry.id))
        ).scalar_one()

    client.post("/auth/logout")
    login = client.post(
        "/auth/login",
        json={
            "email": "g72-admin-no-delegation@example.com",
            "password": password,
        },
    )
    assert login.status_code == 200, login.text

    # An admin identity is not implicitly authorized to submit work as Owner.
    response = client.post(path, json=payload)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "PERMISSION_DENIED"

    with sessions() as db:
        after_tasks = db.execute(select(func.count(DBGenerationTask.id))).scalar_one()
        after_events = db.execute(select(func.count(DBGenerationEvent.id))).scalar_one()
        after_usage = db.execute(
            select(func.count(DBGenerationUsageEntry.id))
        ).scalar_one()
        reused_key = db.execute(
            select(DBGenerationTask.id).where(
                DBGenerationTask.idempotency_key == "g72-admin-no-delegation"
            )
        ).scalar_one_or_none()

    assert after_tasks == before_tasks
    assert after_events == before_events
    assert after_usage == before_usage
    assert reused_key is None

def test_g72_foreign_tenant_admin_cannot_preflight_create_or_read(generation_context):
    """Foreign-tenant admin is denied without project disclosure or task side effects."""
    client, sessions, _app, media = generation_context
    project = create_project(client, "G72 foreign-tenant admin isolation")
    asset_id = _g72_fake_visual_fixture(sessions, project)
    payload = _g72_fake_payload(
        client, project, asset_id, key="g72-foreign-admin-isolated"
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    password = secrets.token_urlsafe(24)
    with sessions() as db:
        tenant = DBTenant(
            id="g72-foreign-admin-tenant", name="G72 Foreign Admin",
            slug="g72-foreign-admin", project_quota=50,
            storage_quota_bytes=1024**3, used_storage_bytes=0,
            concurrent_render_quota=2, monthly_render_seconds_quota=3600,
            render_seconds_used=0, quota_period=now.strftime("%Y-%m"),
            created_at=now, updated_at=now,
        )
        user = DBUser(
            id="g72-foreign-admin", tenant_id=tenant.id,
            email="g72-foreign-admin@example.com",
            display_name="Foreign Tenant Admin",
            password_hash=hash_password(password), role="admin",
            is_active=True, created_at=now, updated_at=now,
        )
        db.add_all([tenant, user])
        db.commit()
        before_tasks = db.execute(select(func.count(DBGenerationTask.id))).scalar_one()
        before_events = db.execute(select(func.count(DBGenerationEvent.id))).scalar_one()
        before_usage = db.execute(
            select(func.count(DBGenerationUsageEntry.id))
        ).scalar_one()
        before_assets = db.execute(
            select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == project["id"]
            )
        ).scalar_one()

    assert client.post("/auth/logout").status_code == 204
    login = client.post(
        "/auth/login",
        json={"email": "g72-foreign-admin@example.com", "password": password},
    )
    assert login.status_code == 200, login.text

    base = f"/projects/{project['id']}/talking-head/fake-generation-tasks"
    for endpoint in (base + "/validate", base):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "PERMISSION_DENIED"
    assert client.get(f"/projects/{project['id']}").status_code == 404

    with sessions() as db:
        assert db.execute(select(func.count(DBGenerationTask.id))).scalar_one() == before_tasks
        assert db.execute(select(func.count(DBGenerationEvent.id))).scalar_one() == before_events
        assert db.execute(
            select(func.count(DBGenerationUsageEntry.id))
        ).scalar_one() == before_usage
        assert db.execute(
            select(func.count(DBAssetVersion.id)).where(
                DBAssetVersion.project_id == project["id"]
            )
        ).scalar_one() == before_assets
        assert db.execute(
            select(DBGenerationTask.id).where(
                DBGenerationTask.idempotency_key == "g72-foreign-admin-isolated"
            )
        ).scalar_one_or_none() is None
    assert media.uploads == 0



@pytest.fixture()
def postgres_generation_context():
    database_url = (
        os.environ.get("AETHER_POSTGRES_TEST_DATABASE_URL")
        or os.environ.get("AETHER_TEST_POSTGRES_URL")
    )
    if not database_url:
        pytest.skip("AETHER_POSTGRES_TEST_DATABASE_URL is not configured")

    engine = build_engine(database_url)
    Base.metadata.drop_all(bind=engine)
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def get_db():
        with sessions() as db:
            yield db

    app = create_app(
        app_engine=engine,
        db_dependency=get_db,
        video_use_adapter=FakeGeneratedMediaStore(),
        bootstrap_admin_password=OWNER_PASSWORD,
        bootstrap_admin_email="g76-postgres-owner@example.com",
        worker_token=WORKER_TOKEN,
        cookie_secure=False,
        enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    app.state.moneyprinter = MagicMock()
    try:
        with LeaseAwareTestClient(app) as client:
            assert client.post(
                "/auth/login",
                json={"email": "g76-postgres-owner@example.com", "password": OWNER_PASSWORD},
            ).status_code == 200
            client.headers.update({"X-Aether-CSRF": "1"})
            yield client, sessions, engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_g76_postgres_task_row_lock_serializes_heartbeat_and_owner_cancel(
    postgres_generation_context,
):
    """Real PostgreSQL row locks must serialize Worker heartbeat with Owner cancellation."""
    client, sessions, engine = postgres_generation_context
    project = create_project(client, "G76 PostgreSQL row-lock race")
    task = create_task(client, project, key="g76-postgres-row-lock-race")
    worker_id = "worker-g76-postgres"
    claimed = claim(client, worker_id)
    assert claimed.status_code == 200, claimed.text
    task_id = claimed.json()["taskId"]

    blocker = sessions()
    blocker.execute(
        select(DBGenerationTask)
        .where(DBGenerationTask.id == task_id)
        .with_for_update()
    ).scalar_one()

    locked_task_queries = []
    query_lock = threading.Lock()
    both_routes_waiting_on_row_lock = threading.Event()

    def observe_task_row_lock(_conn, _cursor, statement, _parameters, _context, _executemany):
        normalized = statement.lower()
        if "from generation_tasks" in normalized and "for update" in normalized:
            with query_lock:
                locked_task_queries.append(statement)
                if len(locked_task_queries) >= 2:
                    both_routes_waiting_on_row_lock.set()

    event.listen(engine, "before_cursor_execute", observe_task_row_lock)
    pool = ThreadPoolExecutor(max_workers=2)
    heartbeat_future = None
    cancel_future = None
    try:
        heartbeat_future = pool.submit(
            client.post,
            f"/internal/generation-tasks/{task_id}/heartbeat",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
            json={},
        )
        cancel_future = pool.submit(
            client.post,
            f"/projects/{project['id']}/generation-tasks/{task_id}/cancel",
        )

        assert both_routes_waiting_on_row_lock.wait(10), (
            "heartbeat and Owner cancel did not both issue SELECT ... FOR UPDATE "
            f"against generation_tasks; observed={locked_task_queries}"
        )
        assert not heartbeat_future.done(), "heartbeat did not wait for the held PostgreSQL row lock"
        assert not cancel_future.done(), "Owner cancel did not wait for the held PostgreSQL row lock"

        blocker.commit()
        heartbeat_response = heartbeat_future.result(timeout=15)
        cancel_response = cancel_future.result(timeout=15)
    finally:
        if blocker.in_transaction():
            blocker.rollback()
        event.remove(engine, "before_cursor_execute", observe_task_row_lock)
        pool.shutdown(wait=True, cancel_futures=True)
        blocker.close()

    assert cancel_response.status_code == 200, cancel_response.text
    assert heartbeat_response.status_code in {200, 409}, heartbeat_response.text
    with sessions() as db:
        persisted = db.execute(
            select(DBGenerationTask).where(DBGenerationTask.id == task_id)
        ).scalar_one()
        assert persisted.status == "CANCELED"
        assert persisted.cancel_requested_at is not None
        assert persisted.lease_owner is None
        assert persisted.lease_expires_at is None

def test_g75_ineligible_provider_candidate_does_not_pin_half_open_probe(provider_control_context):
    """An ineligible FIFO candidate must not consume the tenant's HALF_OPEN probe."""
    client, sessions, app, _ = provider_control_context
    # Synthetic config/attestation only; no Provider adapter call is allowed.
    app.state.moneyprinter = MagicMock()
    config = publish_provider_config(client, create_provider_config(client))
    assert attest_provider(client, sessions, config).status_code == 201

    project = create_project(client, "G75 half-open queue candidates")
    first = create_task(client, project, key="g75-stale-config")
    now = datetime.datetime.now(datetime.timezone.utc)
    eligible_task_id = str(uuid.uuid4())
    eligible_created_at = now - datetime.timedelta(seconds=1)

    with sessions() as db:
        stale_task = db.get(DBGenerationTask, first["taskId"])
        assert stale_task is not None
        assert stale_task.provider == "moneyprinter"
        assert stale_task.capability_snapshot_json["mode"] == "moneyprinter"
        assert stale_task.capability_snapshot_json["configVersionId"] == config["id"]

        # Keep the private one-task canary quota intact: seed the second queued
        # candidate directly as a synthetic scheduler fixture, not a second API
        # submission or an additional reservation.
        eligible_snapshot = dict(stale_task.capability_snapshot_json)
        stale_snapshot = dict(eligible_snapshot)
        stale_snapshot["configVersionId"] = "superseded-config-version"
        stale_task.capability_snapshot_json = stale_snapshot
        stale_task.created_at = now - datetime.timedelta(seconds=2)

        eligible_task = DBGenerationTask(
            id=eligible_task_id,
            tenant_id=stale_task.tenant_id,
            project_id=project["id"],
            requested_by=stale_task.requested_by,
            provider=stale_task.provider,
            status="QUEUED",
            progress=0,
            message="Synthetic eligible G75 queue candidate",
            request_json=dict(stale_task.request_json),
            request_hash=stale_task.request_hash,
            capability_snapshot_json=eligible_snapshot,
            capability_snapshot_hash=stale_task.capability_snapshot_hash,
            idempotency_key=idempotency_uuid("g75-eligible-synthetic-row"),
            upstream_job_id=None,
            provider_artifact_id=None,
            media_id=None,
            asset_version_id=None,
            attempts=1,
            max_attempts=stale_task.max_attempts,
            lease_owner=None,
            lease_epoch=0,
            circuit_probe_epoch=None,
            lease_expires_at=None,
            cancel_requested_at=None,
            error_code=None,
            error_message=None,
            created_at=eligible_created_at,
            updated_at=eligible_created_at,
            started_at=None,
            completed_at=None,
        )
        db.add(eligible_task)
        db.add(DBGenerationAttempt(
            id=str(uuid.uuid4()),
            generation_task_id=eligible_task_id,
            attempt_no=1,
            status="QUEUED",
            submission_started_at=None,
            upstream_job_id=None,
            reconciliation_state="NOT_SUBMITTED",
            error_code=None,
            error_message=None,
            created_at=eligible_created_at,
            completed_at=None,
        ))
        db.add(DBGenerationCircuitState(
            id=str(uuid.uuid4()),
            tenant_id=stale_task.tenant_id,
            provider="moneyprinter",
            state="HALF_OPEN",
            failure_timestamps_json=[],
            opened_at=now - datetime.timedelta(minutes=2),
            cooldown_until=now - datetime.timedelta(seconds=1),
            half_open_task_id=None,
            probe_epoch=7,
            half_open_lease_epoch=None,
            half_open_issuer=None,
            disabled_reason_code=None,
            updated_at=now,
        ))
        db.commit()

    claimed = claim(client, worker_id="worker-proof")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == eligible_task_id
    assert claimed.json()["leaseEpoch"] == 1

    with sessions() as db:
        stale_task = db.get(DBGenerationTask, first["taskId"])
        eligible_task = db.get(DBGenerationTask, eligible_task_id)
        circuit = db.execute(
            select(DBGenerationCircuitState).where(
                DBGenerationCircuitState.tenant_id == eligible_task.tenant_id,
                DBGenerationCircuitState.provider == "moneyprinter",
            )
        ).scalar_one()
        assert stale_task.lease_owner is None
        assert int(stale_task.lease_epoch or 0) == 0
        assert eligible_task.lease_owner == "worker-proof"
        assert eligible_task.lease_epoch == 1
        assert eligible_task.circuit_probe_epoch == 8
        assert circuit.state == "HALF_OPEN"
        assert circuit.probe_epoch == 8
        assert circuit.half_open_task_id == eligible_task.id
        assert circuit.half_open_lease_epoch == eligible_task.lease_epoch
        assert circuit.half_open_issuer == "worker-proof"

    app.state.moneyprinter.assert_not_called()

def test_g73_keyset_claim_scans_past_foreign_tenant_ineligible_queue_rows(generation_context):
    """A foreign tenant's older wrong-mode backlog must not starve an eligible FAKE task."""
    client, sessions, _app, media = generation_context
    project = create_project(client, "G73 eligible tenant queue")
    asset_id = _g72_fake_visual_fixture(sessions, project)
    request = _g72_fake_payload(client, project, asset_id, key="g73-cross-tenant-eligible")
    created = client.post(
        f"/projects/{project['id']}/talking-head/fake-generation-tasks",
        json=request,
    )
    assert created.status_code == 202, created.text
    eligible_id = created.json()["taskId"]
    now = datetime.datetime.now(datetime.timezone.utc)
    foreign_password = secrets.token_urlsafe(24)

    with sessions() as db:
        eligible = db.get(DBGenerationTask, eligible_id)
        assert eligible is not None
        current_tenant_id = eligible.tenant_id
        first_created = eligible.created_at - datetime.timedelta(days=1)

        foreign_tenant = DBTenant(
            id="g73-foreign-tenant",
            name="G73 Foreign Tenant",
            slug="g73-foreign-tenant",
            project_quota=50,
            storage_quota_bytes=1024**3,
            used_storage_bytes=0,
            concurrent_render_quota=2,
            monthly_render_seconds_quota=3600,
            render_seconds_used=0,
            quota_period=now.strftime("%Y-%m"),
            created_at=now,
            updated_at=now,
        )
        foreign_owner = DBUser(
            id="g73-foreign-owner",
            tenant_id=foreign_tenant.id,
            email="g73-foreign-owner@example.com",
            display_name="G73 Foreign Tenant Owner",
            password_hash=hash_password(foreign_password),
            role="owner",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        foreign_project = DBProject(
            id="g73-foreign-project",
            tenant_id=foreign_tenant.id,
            owner_id=foreign_owner.id,
            name="G73 foreign tenant ineligible backlog",
            timeline={"version": "1.1", "output": None, "tracks": []},
            materials=[],
            revision=1,
            created_at=now,
            updated_at=now,
        )
        old_rows = []
        for idx in range(205):
            created_at = first_created + datetime.timedelta(microseconds=idx)
            old_rows.append(DBGenerationTask(
                id=str(uuid.uuid4()),
                tenant_id=foreign_tenant.id,
                project_id=foreign_project.id,
                requested_by=foreign_owner.id,
                provider="moneyprinter",
                status="QUEUED",
                progress=0,
                message="synthetic foreign-tenant wrong-mode queue row",
                request_json={"syntheticFixtureOnly": True},
                request_hash=f"g73-foreign-fixture-{idx}",
                capability_snapshot_json={"mode": "moneyprinter"},
                capability_snapshot_hash="0" * 64,
                idempotency_key=f"g73-foreign-idempotency-{idx}",
                attempts=1,
                max_attempts=1,
                lease_owner=None,
                lease_epoch=0,
                circuit_probe_epoch=None,
                lease_expires_at=None,
                cancel_requested_at=None,
                error_code=None,
                error_message=None,
                created_at=created_at,
                updated_at=created_at,
            ))
        db.add_all([foreign_tenant, foreign_owner, foreign_project, *old_rows])
        db.commit()

    claimed = claim(client, worker_id="g73-cross-tenant-safe-worker")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["taskId"] == eligible_id
    assert claimed.json()["providerMode"] == "talking-head-fake"

    with sessions() as db:
        eligible = db.get(DBGenerationTask, eligible_id)
        assert eligible.tenant_id == current_tenant_id
        assert eligible.status == "SUBMITTING"
        assert eligible.lease_owner == "g73-cross-tenant-safe-worker"
        foreign_queued = db.execute(
            select(func.count(DBGenerationTask.id)).where(
                DBGenerationTask.tenant_id == "g73-foreign-tenant",
                DBGenerationTask.status == "QUEUED",
                DBGenerationTask.lease_owner.is_(None),
            )
        ).scalar_one()
        assert foreign_queued == 205
        foreign_claim_events = db.execute(
            select(func.count(DBGenerationEvent.id)).where(
                DBGenerationEvent.generation_task_id.in_(
                    select(DBGenerationTask.id).where(
                        DBGenerationTask.tenant_id == "g73-foreign-tenant"
                    )
                ),
                DBGenerationEvent.event_type == "TASK_CLAIMED",
            )
        ).scalar_one()
        assert foreign_claim_events == 0

    assert media.uploads == 0
