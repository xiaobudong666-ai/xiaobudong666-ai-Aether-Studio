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
import uuid

sys.path.insert(0, "/app")
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal, engine, get_db
from app.main import create_app
from app.models import (
    DBAssetVersion, DBGenerationCircuitState, DBGenerationEvent,
    DBGenerationProviderEvent, DBGenerationTask, DBGenerationUsageEntry,
    DBProject, DBRightsSnapshot, DBTenant,
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
