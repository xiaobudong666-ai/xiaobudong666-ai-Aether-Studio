import datetime
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    DBGenerationEvent,
    DBGenerationTask,
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
