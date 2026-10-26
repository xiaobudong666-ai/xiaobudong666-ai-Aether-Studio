"""G73-G76 FAKE-only PostgreSQL circuit probe ownership smoke.

Uses real API + disposable PostgreSQL + Sidecar with synthetic media. No real
Provider, no release deployment, and no financial-policy mutation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

sys.path.insert(0, "/app")
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.database import SessionLocal, engine, get_db
from app.generation_tasks import (
    MONEYPRINTER_ADAPTER_VERSION, MONEYPRINTER_UPSTREAM_PIN, sha256_json,
)
from app.main import create_app
from app.models import (
    DBAssetVersion, DBGenerationAttempt, DBGenerationCircuitState,
    DBGenerationEvent, DBGenerationProviderAttestation,
    DBGenerationProviderConfigVersion, DBGenerationProviderEvent,
    DBGenerationTask, DBGenerationUsageEntry, DBProject, DBRightsSnapshot,
    DBTenant, DBUser,
)
from app.video_use_adapter import VideoUseAdapter

INPUT = Path("/tmp/g76-circuit-original.mp4")
VIDEO_USE = "http://video-use:8002"
WORKER_TOKEN = os.environ["AETHER_WORKER_TOKEN"]


def guard():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert os.getenv("DATABASE_URL") == (
        "postgresql+psycopg://ci_g72:ci-only-postgres-password@postgres-g72:5432/aether_g72_ci"
    )
    assert engine.dialect.name == "postgresql"
    assert os.getenv("VIDEO_USE_API_URL") == VIDEO_USE
    assert WORKER_TOKEN.startswith("ci-")


def owner_client():
    app = create_app(
        app_engine=engine, db_dependency=get_db,
        video_use_adapter=VideoUseAdapter(api_url=VIDEO_USE, max_retries=0),
        cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="deterministic-fake",
    )
    return TestClient(app)


def login(client):
    response = client.post("/auth/login", json={
        "email": os.getenv("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"),
        "password": os.environ["AETHER_BOOTSTRAP_ADMIN_PASSWORD"],
    })
    assert response.status_code == 200, response.text
    client.headers["X-Aether-CSRF"] = "1"


def seed_image(project_id: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        source_id = str(uuid.uuid4())
        db.add(DBAssetVersion(
            id=source_id, tenant_id=project.tenant_id, project_id=project_id,
            media_id="synthetic-g76-source-" + uuid.uuid4().hex,
            version_no=1, sha256="d" * 64, media_type="image",
            content_type="image/png", size_bytes=100,
            probe_json={"image": {"width": 48, "height": 48}},
            created_by=project.owner_id, created_at=now,
        ))
        db.add(DBRightsSnapshot(
            id=str(uuid.uuid4()), tenant_id=project.tenant_id, project_id=project_id,
            asset_version_id=source_id, status="ALLOWED", purpose="REMIX",
            territory="TEST", valid_from=None, valid_until=None,
            evidence_ref="SYNTHETIC_CI_G76_ALLOWED",
            captured_by=project.owner_id, captured_at=now,
        ))
        db.commit()
        return source_id


def set_circuit(tenant_id: str, state: str, *, half_open_task_id=None, cooldown_until=None):
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        row = db.execute(select(DBGenerationCircuitState).where(
            DBGenerationCircuitState.tenant_id == tenant_id,
            DBGenerationCircuitState.provider == "moneyprinter",
        )).scalar_one_or_none()
        if row is None:
            row = DBGenerationCircuitState(
                id=str(uuid.uuid4()), tenant_id=tenant_id, provider="moneyprinter",
                state=state, failure_timestamps_json=[], updated_at=now,
            )
            db.add(row)
        row.state = state
        row.failure_timestamps_json = [now.isoformat()] if state in {"OPEN", "HALF_OPEN"} else []
        row.opened_at = now if state in {"OPEN", "HALF_OPEN"} else None
        row.cooldown_until = cooldown_until
        row.half_open_task_id = half_open_task_id
        row.updated_at = now
        db.commit()


def circuit_snapshot(tenant_id: str) -> dict:
    with SessionLocal() as db:
        row = db.execute(select(DBGenerationCircuitState).where(
            DBGenerationCircuitState.tenant_id == tenant_id,
            DBGenerationCircuitState.provider == "moneyprinter",
        )).scalar_one()
        return {
            "state": row.state,
            "halfOpenTaskId": row.half_open_task_id,
            "cooldownUntil": row.cooldown_until.isoformat() if row.cooldown_until else None,
            "openedAt": row.opened_at.isoformat() if row.opened_at else None,
        }


def closed_event_count(tenant_id: str) -> int:
    with SessionLocal() as db:
        return db.execute(select(func.count(DBGenerationProviderEvent.id)).where(
            DBGenerationProviderEvent.tenant_id == tenant_id,
            DBGenerationProviderEvent.event_type == "GENERATION_CIRCUIT_CLOSED",
        )).scalar_one()


def create_task(client, project_id: str, source_id: str, key: str) -> str:
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        assert project is not None
        revision = project.revision
    caps = client.get("/generation/providers/moneyprinter/capabilities")
    assert caps.status_code == 200, caps.text
    payload = {
        "text": "G76 synthetic circuit probe ownership " + key,
        "durationSeconds": 1, "aspectRatio": "9:16",
        "resolution": "720p", "voiceId": "synthetic-seed",
        "imageAssetVersionId": source_id,
        "idempotencyKey": str(uuid.uuid4()),
        "expectedProjectRevision": revision,
        "capabilitySnapshotHash": caps.json()["snapshotHash"],
        "confirmSyntheticTestOnly": True,
    }
    base = f"/projects/{project_id}/talking-head/fake-generation-tasks"
    for _ in range(3):
        preflight = client.post(base + "/validate", json=payload)
        if preflight.status_code == 409 and preflight.json().get("detail", {}).get("code") == "CAPABILITY_SNAPSHOT_UNKNOWN":
            caps = client.get("/generation/providers/moneyprinter/capabilities")
            assert caps.status_code == 200, caps.text
            payload["capabilitySnapshotHash"] = caps.json()["snapshotHash"]
            continue
        assert preflight.status_code == 200, preflight.text
        response = client.post(base, json=payload)
        if response.status_code == 409 and response.json().get("detail", {}).get("code") == "CAPABILITY_SNAPSHOT_UNKNOWN":
            caps = client.get("/generation/providers/moneyprinter/capabilities")
            assert caps.status_code == 200, caps.text
            payload["capabilitySnapshotHash"] = caps.json()["snapshotHash"]
            continue
        assert response.status_code == 202, response.text
        return response.json()["taskId"]
    raise AssertionError("Capability snapshot kept rotating")


def claim(client, task_id: str, worker_id: str):
    response = client.post("/internal/generation-tasks/claim", headers={
        "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id,
    })
    assert response.status_code == 200, response.text
    assert response.json()["taskId"] == task_id, response.json()


def finish(client, task_id: str, worker_id: str, suffix: str, media_bytes: bytes):
    upstream = "g76-upstream-" + suffix
    artifact = "g76-artifact-" + suffix
    headers = {"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id}
    for status, progress, provider_artifact in (
        ("RUNNING", 5, None), ("INGESTING", 90, artifact),
    ):
        response = client.post(
            f"/internal/generation-tasks/{task_id}/transition",
            headers=headers,
            json={
                "status": status, "progress": progress,
                "message": "Synthetic G76 circuit ownership test",
                "upstreamJobId": upstream, "providerArtifactId": provider_artifact,
            },
        )
        assert response.status_code == 200, response.text
    response = client.post(
        f"/internal/generation-tasks/{task_id}/artifact-intake",
        headers=headers, data={"providerArtifactId": artifact},
        files={"file": (suffix + ".mp4", media_bytes, "video/mp4")},
    )
    assert response.status_code == 201, response.text


def verify_g73_keyset_fairness(
    client, project_id: str, source_id: str, tenant_id: str, ineligible_count: int = 205
):
    """Real PostgreSQL proof that >2 pages of older wrong-mode rows cannot starve G72."""
    eligible_id = create_task(client, project_id, source_id, "g73-pg-keyset-eligible")
    fixture_ids = [str(uuid.uuid4()) for _ in range(ineligible_count)]
    with SessionLocal() as db:
        project = db.get(DBProject, project_id)
        eligible = db.get(DBGenerationTask, eligible_id)
        assert project is not None and eligible is not None
        eligible_created_at = eligible.created_at
        rows = []
        for idx, fixture_id in enumerate(fixture_ids):
            created_at = eligible_created_at - dt.timedelta(
                seconds=ineligible_count - idx
            )
            rows.append(DBGenerationTask(
                id=fixture_id, tenant_id=tenant_id, project_id=project_id,
                requested_by=project.owner_id, provider="moneyprinter",
                status="QUEUED", progress=0,
                message="synthetic G73 wrong-mode queue row",
                request_json={"syntheticFixtureOnly": True},
                request_hash=f"g73-pg-fixture-{idx}-{fixture_id}",
                capability_snapshot_json={"mode": "moneyprinter"},
                capability_snapshot_hash="0" * 64,
                idempotency_key=f"g73-pg-fixture-{fixture_id}",
                attempts=1, max_attempts=1,
                created_at=created_at, updated_at=created_at,
            ))
        db.add_all(rows)
        db.commit()

    response = client.post("/internal/generation-tasks/claim", headers={
        "X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": "g73-pg-keyset-worker",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["taskId"] == eligible_id, body
    assert body["providerMode"] == "talking-head-fake", body

    with SessionLocal() as db:
        eligible = db.get(DBGenerationTask, eligible_id)
        assert eligible is not None and eligible.status == "SUBMITTING"
        assert eligible.lease_owner == "g73-pg-keyset-worker"
        untouched = db.execute(select(func.count(DBGenerationTask.id)).where(
            DBGenerationTask.id.in_(fixture_ids),
            DBGenerationTask.status == "QUEUED",
        )).scalar_one()
        assert untouched == ineligible_count, {
            "expectedIneligible": ineligible_count, "untouched": untouched,
        }
        for fixture_id in fixture_ids:
            row = db.get(DBGenerationTask, fixture_id)
            if row is not None:
                db.delete(row)
        db.commit()

    print(json.dumps({
        "phase": "g73_postgresql_keyset_fairness",
        "database": "postgresql",
        "ineligibleOlderWrongModeRows": ineligible_count,
        "eligibleTaskClaimed": eligible_id,
        "realProviderCalled": False,
        "financialSettlementTrusted": False,
    }), flush=True)

def seed_g75_moneyprinter_tenant(now: dt.datetime) -> dict:
    """Create synthetic policy/attestation rows for claim-only G75 tests."""
    suffix = uuid.uuid4().hex
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    config_id = str(uuid.uuid4())
    policy = {
        "enabledIntent": True,
        "concurrentTaskLimit": 1,
        "monthlyRequestLimit": 1,
        "monthlyGeneratedSecondsLimit": 10,
        "maxClipDurationSeconds": 10,
        "maxOutputs": 1,
        "artifactPathPrefixes": ["/tasks/"],
    }
    policy_hash = sha256_json(policy)
    with SessionLocal() as db:
        db.add(DBTenant(
            id=tenant_id, name="G75 synthetic tenant " + suffix[:8],
            slug="g75-" + suffix, quota_period=now.strftime("%Y-%m"),
            created_at=now, updated_at=now,
        ))
        db.flush()
        db.add(DBUser(
            id=user_id, tenant_id=tenant_id,
            email="g75-" + suffix + "@example.invalid",
            display_name="G75 synthetic owner",
            password_hash="synthetic-ci-only-not-a-login-credential",
            role="OWNER", is_active=True, created_at=now, updated_at=now,
        ))
        db.flush()
        db.add(DBProject(
            id=project_id, tenant_id=tenant_id, owner_id=user_id,
            name="G75 synthetic claim-only project",
            timeline={}, materials=[], revision=1,
            created_at=now, updated_at=now,
        ))
        db.flush()
        db.add(DBGenerationProviderConfigVersion(
            id=config_id, tenant_id=tenant_id, provider="moneyprinter",
            version=1, status="PUBLISHED", policy_json=policy,
            policy_hash=policy_hash, created_by=user_id,
            created_at=now, published_by=user_id, published_at=now,
            supersedes_id=None,
        ))
        db.flush()
        for worker_id in (
            "g75-probe-worker", "g75-concurrent-a", "g75-concurrent-b",
        ):
            db.add(DBGenerationProviderAttestation(
                id=str(uuid.uuid4()), tenant_id=tenant_id,
                provider="moneyprinter", worker_id=worker_id,
                operator_mode="moneyprinter", config_version_id=config_id,
                policy_hash=policy_hash,
                adapter_version=MONEYPRINTER_ADAPTER_VERSION,
                upstream_pin=MONEYPRINTER_UPSTREAM_PIN, healthy=True,
                capabilities_json={
                    "credentialState": "PRESENT",
                    "networkIsolation": "ENFORCED",
                    "canaryProfile": "private-one-task-v1",
                    "syntheticOnly": True,
                },
                reason_code="SYNTHETIC_G75_CLAIM_ONLY",
                checked_at=now, expires_at=now + dt.timedelta(minutes=10),
                created_at=now,
            ))
        db.commit()
    return {
        "tenant_id": tenant_id, "user_id": user_id,
        "project_id": project_id, "config_id": config_id,
        "policy_hash": policy_hash,
    }


def seed_g75_moneyprinter_task(
    tenant_id: str, project_id: str, user_id: str,
    config_id: str, policy_hash: str, key: str,
    created_at: dt.datetime,
) -> str:
    task_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    request_json = {"syntheticFixtureOnly": True, "scenario": key}
    snapshot = {
        "mode": "moneyprinter",
        "configVersionId": config_id,
        "policyHash": policy_hash,
    }
    with SessionLocal() as db:
        db.add(DBGenerationTask(
            id=task_id, tenant_id=tenant_id, project_id=project_id,
            requested_by=user_id, provider="moneyprinter", status="QUEUED",
            progress=0, message="Synthetic G75 claim-only fixture",
            request_json=request_json, request_hash=sha256_json(request_json),
            capability_snapshot_json=snapshot,
            capability_snapshot_hash=sha256_json(snapshot),
            idempotency_key="g75-" + key + "-" + uuid.uuid4().hex,
            attempts=1, max_attempts=1, created_at=created_at,
            updated_at=created_at,
        ))
        # Flush the parent task before inserting its FK-dependent attempt.
        db.flush()
        db.add(DBGenerationAttempt(
            id=attempt_id, generation_task_id=task_id, attempt_no=1,
            status="QUEUED", reconciliation_state="NOT_SUBMITTED",
            created_at=created_at,
        ))
        db.commit()
    return task_id


def verify_g75_probe_binding_and_lock(
    base_project_id: str, base_tenant_id: str,
) -> None:
    """Exercise claim-only eligibility ordering and PostgreSQL probe row locking."""
    now = dt.datetime.now(dt.timezone.utc)
    with SessionLocal() as db:
        base_project = db.get(DBProject, base_project_id)
        assert base_project is not None
        base_user_id = base_project.owner_id

    synthetic = seed_g75_moneyprinter_tenant(now)
    invalid_task_id = seed_g75_moneyprinter_task(
        base_tenant_id, base_project_id, base_user_id,
        "g75-missing-published-config", "0" * 64, "ineligible-earlier",
        now - dt.timedelta(seconds=20),
    )
    valid_task_id = seed_g75_moneyprinter_task(
        synthetic["tenant_id"], synthetic["project_id"], synthetic["user_id"],
        synthetic["config_id"], synthetic["policy_hash"], "eligible-other-tenant",
        now - dt.timedelta(seconds=10),
    )
    expired_cooldown = now - dt.timedelta(seconds=1)
    set_circuit(
        base_tenant_id, "OPEN", half_open_task_id=None,
        cooldown_until=expired_cooldown,
    )
    set_circuit(synthetic["tenant_id"], "CLOSED")

    # This app is only an in-process claim endpoint harness. The workflow has
    # stopped Worker daemons; all config/attestation values are synthetic.
    # No Provider adapter call, credentials, network egress, or media dispatch.
    claim_app = create_app(
        app_engine=engine, db_dependency=get_db,
        video_use_adapter=VideoUseAdapter(api_url=VIDEO_USE, max_retries=0),
        worker_token=WORKER_TOKEN, cookie_secure=False, enforce_csrf=True,
        generation_provider_mode="moneyprinter",
    )
    claim_client = TestClient(claim_app)
    response = claim_client.post(
        "/internal/generation-tasks/claim",
        headers={
            "X-Worker-Token": WORKER_TOKEN,
            "X-Worker-Id": "g75-probe-worker",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["taskId"] == valid_task_id, body
    assert body["providerMode"] == "moneyprinter", body
    invalid_circuit = circuit_snapshot(base_tenant_id)
    assert invalid_circuit["state"] == "OPEN", invalid_circuit
    assert invalid_circuit["halfOpenTaskId"] is None, invalid_circuit
    set_circuit(base_tenant_id, "CLOSED")

    # Put 101 eligible tasks behind a held circuit-row lock. The claim query
    # locks its first 100 candidate rows, so a second concurrent claimant can
    # reach the same circuit row using the 101st task. Both must wait for the
    # same PostgreSQL row lock; only one may bind the HALF_OPEN probe.
    concurrent_ids = []
    seed_time = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    for idx in range(101):
        concurrent_ids.append(seed_g75_moneyprinter_task(
            synthetic["tenant_id"], synthetic["project_id"], synthetic["user_id"],
            synthetic["config_id"], synthetic["policy_hash"],
            "concurrent-" + str(idx),
            seed_time + dt.timedelta(microseconds=idx),
        ))
    set_circuit(
        synthetic["tenant_id"], "HALF_OPEN", half_open_task_id=None,
        cooldown_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1),
    )

    clients = [TestClient(claim_app), TestClient(claim_app)]
    gate = Barrier(3)
    def post_claim(client, worker_id):
        gate.wait(timeout=10)
        return client.post(
            "/internal/generation-tasks/claim",
            headers={"X-Worker-Token": WORKER_TOKEN, "X-Worker-Id": worker_id},
        )

    lock_db = SessionLocal()
    lock_db.execute(select(DBGenerationCircuitState).where(
        DBGenerationCircuitState.tenant_id == synthetic["tenant_id"],
        DBGenerationCircuitState.provider == "moneyprinter",
    ).with_for_update()).scalar_one()
    observed_two_waiters = False
    responses = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(post_claim, clients[0], "g75-concurrent-a"),
                pool.submit(post_claim, clients[1], "g75-concurrent-b"),
            ]
            gate.wait(timeout=10)
            deadline = time.monotonic() + 12
            max_waiters = 0
            while time.monotonic() < deadline:
                with SessionLocal() as monitor:
                    waiters = monitor.execute(text("""
                        SELECT count(*)::int
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND usename = current_user
                          AND wait_event_type = 'Lock'
                          AND query ILIKE '%generation_circuit_states%'
                          AND pid <> pg_backend_pid()
                    """)).scalar_one()
                max_waiters = max(max_waiters, int(waiters))
                if max_waiters >= 2:
                    observed_two_waiters = True
                    break
                if all(future.done() for future in futures):
                    break
                time.sleep(0.1)
            lock_db.commit()
            responses = [future.result(timeout=20) for future in futures]
    finally:
        lock_db.rollback()
        lock_db.close()

    assert observed_two_waiters, {
        "expectedTwoPostgreSQLCircuitLockWaiters": True,
        "observed": observed_two_waiters,
    }
    successes = [item for item in responses if item.status_code == 200]
    no_claim = [item for item in responses if item.status_code == 204]
    assert len(successes) == 1 and len(no_claim) == 1, [
        (item.status_code, item.text[:300]) for item in responses
    ]
    claimed_id = successes[0].json()["taskId"]
    with SessionLocal() as db:
        claimed_rows = list(db.execute(select(DBGenerationTask).where(
            DBGenerationTask.id.in_(concurrent_ids),
            DBGenerationTask.status == "SUBMITTING",
        )).scalars())
        assert len(claimed_rows) == 1, [row.id for row in claimed_rows]
        assert claimed_rows[0].id == claimed_id
        assert claimed_rows[0].lease_owner in {
            "g75-concurrent-a", "g75-concurrent-b",
        }
    concurrent_circuit = circuit_snapshot(synthetic["tenant_id"])
    assert concurrent_circuit["state"] == "HALF_OPEN", concurrent_circuit
    assert concurrent_circuit["halfOpenTaskId"] == claimed_id, concurrent_circuit

    set_circuit(synthetic["tenant_id"], "CLOSED")
    with SessionLocal() as db:
        fixture_ids = [invalid_task_id, valid_task_id, *concurrent_ids]
        fixtures = list(db.execute(select(DBGenerationTask).where(
            DBGenerationTask.id.in_(fixture_ids)
        )).scalars())
        cleanup_time = dt.datetime.now(dt.timezone.utc)
        for task in fixtures:
            task.status = "FAILED"
            task.message = "Synthetic G75 fixture cleanup"
            task.completed_at = cleanup_time
            task.lease_owner = None
            task.lease_expires_at = None
            task.updated_at = cleanup_time
        attempts = list(db.execute(select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id.in_(fixture_ids)
        )).scalars())
        for attempt in attempts:
            attempt.status = "FAILED"
            attempt.error_code = "SYNTHETIC_TEST_CLEANUP"
            attempt.error_message = "G75 claim-only fixture cleanup"
            attempt.completed_at = cleanup_time
        db.commit()

    print(json.dumps({
        "phase": "g75_ineligible_candidate_no_probe_bind",
        "database": "postgresql",
        "ineligibleTenantCircuitRemainedOpen": invalid_circuit["state"] == "OPEN",
        "ineligibleTenantProbeStayedEmpty": invalid_circuit["halfOpenTaskId"] is None,
        "eligibleOtherTenantTaskClaimed": valid_task_id,
        "realProviderCalled": False,
        "financialSettlementTrusted": False,
    }), flush=True)
    print(json.dumps({
        "phase": "g75_half_open_probe_row_lock_concurrency",
        "database": "postgresql",
        "eligibleConcurrentTasks": len(concurrent_ids),
        "twoCircuitLockWaitersObserved": observed_two_waiters,
        "claimSuccesses": len(successes),
        "claimNoContent": len(no_claim),
        "boundProbeMatchesClaimedTask": concurrent_circuit["halfOpenTaskId"] == claimed_id,
        "realProviderCalled": False,
        "financialSettlementTrusted": False,
    }), flush=True)


def run():
    guard()
    media = INPUT.read_bytes()
    assert len(media) > 1000 and media[4:8] == b"ftyp"
    media_sha = hashlib.sha256(media).hexdigest()
    with owner_client() as client:
        login(client)
        created = client.post("/projects", json={
            "name": "G76 synthetic PostgreSQL circuit " + uuid.uuid4().hex[:12],
        })
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        source_id = seed_image(project_id)
        with SessionLocal() as db:
            tenant_id = db.get(DBProject, project_id).tenant_id
        set_circuit(tenant_id, "CLOSED")
        verify_g73_keyset_fairness(client, project_id, source_id, tenant_id)
        verify_g75_probe_binding_and_lock(project_id, tenant_id)

        # An older claimed task succeeding while the breaker is OPEN must not
        # clear the cooldown or emit a CLOSED event.
        open_task = create_task(client, project_id, source_id, "late-open")
        claim(client, open_task, "g76-worker-open")
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=3)
        set_circuit(tenant_id, "OPEN", cooldown_until=deadline)
        closed_before_open = closed_event_count(tenant_id)
        finish(client, open_task, "g76-worker-open", "late-open", media)
        open_after = circuit_snapshot(tenant_id)
        assert open_after["state"] == "OPEN", open_after
        assert open_after["halfOpenTaskId"] is None, open_after
        assert open_after["cooldownUntil"] == deadline.isoformat(), open_after
        assert closed_event_count(tenant_id) == closed_before_open

        # Prepare two claims while CLOSED, then make only the second task the
        # HALF_OPEN probe. A stale first completion must not heal it; the exact
        # current probe must close once and clear the probe identity.
        set_circuit(tenant_id, "CLOSED")
        old_task = create_task(client, project_id, source_id, "old-not-probe")
        probe_task = create_task(client, project_id, source_id, "current-probe")
        claim(client, old_task, "g76-worker-old")
        claim(client, probe_task, "g76-worker-probe")
        set_circuit(tenant_id, "HALF_OPEN", half_open_task_id=probe_task)
        closed_before_probe = closed_event_count(tenant_id)
        finish(client, old_task, "g76-worker-old", "wrong-probe", media)
        wrong_probe = circuit_snapshot(tenant_id)
        assert wrong_probe["state"] == "HALF_OPEN", wrong_probe
        assert wrong_probe["halfOpenTaskId"] == probe_task, wrong_probe
        assert closed_event_count(tenant_id) == closed_before_probe
        finish(client, probe_task, "g76-worker-probe", "right-probe", media)
        probe_after = circuit_snapshot(tenant_id)
        assert probe_after["state"] == "CLOSED", probe_after
        assert probe_after["halfOpenTaskId"] is None, probe_after
        assert closed_event_count(tenant_id) == closed_before_probe + 1

        print(json.dumps({
            "phase": "g76_postgres_late_success_and_probe_ownership",
            "fakeOnly": True, "database": "postgresql", "mediaBytes": len(media),
            "mediaSha256": media_sha,
            "openLateSuccess": {"stateRemainedOpen": True, "cooldownPreserved": True, "closedEventDelta": 0},
            "wrongProbeSuccess": {"stateRemainedHalfOpen": True, "probeIdentityPreserved": True, "closedEventDelta": 0},
            "currentProbeSuccess": {"stateClosed": True, "probeCleared": True, "closedEventDelta": 1},
            "realProviderCalled": False, "financialSettlementTrusted": False,
        }), flush=True)


if __name__ == "__main__":
    run()
