import datetime
import inspect
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
    DBGenerationUsageEntry,
    DBProject,
    DBTenant,
    DBUser,
)
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

OWNER_PASSWORD = secrets.token_urlsafe(24)
WORKER_TOKEN = secrets.token_urlsafe(32)


class FakeGeneratedMediaStore:
    def __init__(self):
        self.uploads = 0
        self.fail_probe = False
        self.interrupt = False

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
        return {
            "mediaId": f"generated-media-{self.uploads}",
            "projectId": project_id,
            "fileName": filename,
            "contentType": content_type,
            "metadata": metadata,
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
    with TestClient(app) as client:
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
    with TestClient(app) as client:
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
    with TestClient(app) as client:
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


def test_32_rights_default_block_then_allowed_is_derived_without_mutating_evidence(generation_context):
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
    assert refreshed["status"] == "SUCCEEDED"
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
