"""Disposable G72 PG table-lock race gate. FAKE-ONLY, synthetic rights.

Execute in the disposable PostgreSQL API container. This is not trusted M12
issuance, delegated-Owner acceptance, or production evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import runpy
import sys
import time
import uuid
from sqlalchemy import func, select, text

# CI copies this file to /tmp; native API modules live in /app.
sys.path.insert(0, "/app")

STATE = Path("/tmp/g72-pg-native-worker-lock-race.json")
REVOKED_STATE = Path("/tmp/g72-pg-native-worker-revoked-claim.json")
SMOKE = Path("/tmp/g72-compose-network-smoke.py")
PG = ("postgresql+psycopg://ci_g72:ci-only-postgres-password@"
      "postgres-g72:5432/aether_g72_ci")


def guard():
    assert os.getenv("AETHER_FAKE_ONLY_NONPROD") == "true"
    assert os.getenv("AETHER_RUNTIME_STAGE") == "nonproduction"
    assert os.getenv("AETHER_GENERATION_PROVIDER_MODE") == "deterministic-fake"
    assert os.getenv("DATABASE_URL") == PG
    assert os.getenv("AETHER_WORKER_TOKEN", "").startswith("ci-")
    assert os.getenv("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "").startswith("ci-")
    assert SMOKE.is_file()


def submit(state_path: Path = STATE):
    from app.database import SessionLocal
    from app.models import DBGenerationTask
    smoke = runpy.run_path(str(SMOKE))
    with smoke["authenticate"]() as client:
        response = client.post("/projects", json={
            "name": "G72 PG native claim race " + uuid.uuid4().hex[:12]})
        assert response.status_code == 201, response.text[:240]
        project = response.json()
        pid = project["id"]
        image = smoke["seed_test_image_and_remix_rights"](pid)
        caps = client.get("/generation/providers/moneyprinter/capabilities")
        assert caps.status_code == 200
        payload = {
            "text": "Synthetic PostgreSQL table lock dual Worker race",
            "durationSeconds": 1, "aspectRatio": "9:16",
            "resolution": "720p", "voiceId": "synthetic-seed",
            "imageAssetVersionId": image, "idempotencyKey": str(uuid.uuid4()),
            "expectedProjectRevision": project["revision"],
            "capabilitySnapshotHash": caps.json()["snapshotHash"],
            "confirmSyntheticTestOnly": True,
        }
        endpoint = f"/projects/{pid}/talking-head/fake-generation-tasks"
        assert client.post(endpoint + "/validate", json=payload).status_code == 200
        created = client.post(endpoint, json=payload)
        assert created.status_code == 202, created.text[:240]
        task_id = created.json()["taskId"]
    with SessionLocal() as db:
        row = db.get(DBGenerationTask, task_id)
        assert row and row.status == "QUEUED" and row.lease_owner is None
    assert not state_path.exists(), "state must be disposable and unique"
    state_path.write_text(json.dumps({"projectId": pid, "taskId": task_id,
                                      "sourceId": image}))
    print("G72_PG_LOCK_RACE_TASK_QUEUED", flush=True)


def lock(inject_revoke: bool = False):
    from app.database import engine
    assert engine.dialect.name == "postgresql"
    # ACCESS EXCLUSIVE holds both real API Worker-claim SQL statements at the
    # same table. Release only after PG confirms two distinct waiting backends.
    with engine.begin() as held:
        held.execute(text("LOCK TABLE generation_tasks IN ACCESS EXCLUSIVE MODE"))
        print("G72_PG_LOCK_RACE_LOCK_HELD", flush=True)
        deadline = time.monotonic() + 110
        n = 0
        while time.monotonic() < deadline:
            with engine.connect() as probe:
                n = int(probe.scalar(text(
                    "SELECT count(DISTINCT pid) FROM pg_locks "
                    "WHERE relation='generation_tasks'::regclass AND NOT granted"
                )) or 0)
            if n >= 2:
                print("G72_PG_LOCK_RACE_TWO_CLAIMS_WAITING", flush=True)
                if inject_revoke:
                    # The claims are blocked on generation_tasks while a
                    # separate SQL transaction durably revokes REMIX rights.
                    # The physical Sidecar remains untouched.
                    from app.database import SessionLocal
                    from app.models import DBAssetVersion, DBRightsSnapshot
                    state = json.loads(REVOKED_STATE.read_text())
                    with SessionLocal() as db:
                        source = db.get(DBAssetVersion, state["sourceId"])
                        assert source and source.project_id == state["projectId"]
                        previous = db.execute(select(DBRightsSnapshot).where(
                            DBRightsSnapshot.asset_version_id == source.id,
                            DBRightsSnapshot.purpose == "REMIX",
                        ).order_by(DBRightsSnapshot.captured_at.desc())).scalars().first()
                        assert previous and previous.status == "ALLOWED"
                        db.add(DBRightsSnapshot(
                            id=str(uuid.uuid4()), tenant_id=source.tenant_id,
                            project_id=source.project_id, asset_version_id=source.id,
                            status="REVOKED", purpose="REMIX", territory="TEST",
                            valid_from=None, valid_until=None,
                            evidence_ref="SYNTHETIC_G72_PG_TWO_LIVE_DAEMON_REVOKE",
                            captured_by=previous.captured_by,
                            captured_at=dt.datetime.now(dt.timezone.utc)
                                + dt.timedelta(seconds=2),
                        ))
                        db.commit()
                    print("G72_PG_TWO_CLAIM_WAITERS_REVOKED_BEFORE_UNLOCK",
                          flush=True)
                break
            time.sleep(.1)
        else:
            raise AssertionError(f"two PG claim waiters required, observed {n}")
    print("G72_PG_LOCK_RACE_RELEASED", flush=True)


def verify():
    smoke = runpy.run_path(str(SMOKE))
    saved = json.loads(STATE.read_text())
    with smoke["authenticate"]() as client:
        deadline = time.monotonic() + 180
        status = "QUEUED"
        while time.monotonic() < deadline:
            row = smoke["get_result"](
                client, saved["projectId"], saved["taskId"])
            status = row["status"]
            if status == "RIGHTS_BLOCKED":
                break
            assert status not in {"FAILED", "CANCELED", "UNKNOWN"}, status
            time.sleep(1)
        else:
            raise AssertionError(f"native Worker completion timeout: {status}")
    receipt = smoke["check_asset_and_receipts"](
        saved["projectId"], saved["taskId"])
    assert receipt["claimedEvents"] == receipt["attempts"] == 1
    assert receipt["ingestEvents"] == receipt["settledEntries"] == 1
    print(json.dumps({
        "gate": "G72_PG_NATIVE_WORKER_LOCK_RACE_PASS",
        "rights": "SYNTHETIC_ONLY", "receipt": receipt,
    }), flush=True)



def verify_revoked():
    """Synthetic rights revoked while two daemon claims wait in real PG.

    SAFETY ONLY: the current API skips revoked queued tasks, which leaves them
    QUEUED with RESERVED usage. This deliberately does not mark G72 closed or
    decide any Owner-controlled cancellation/refund policy.
    """
    from app.database import SessionLocal
    from app.models import (
        DBAssetVersion, DBGenerationTask, DBGenerationEvent,
        DBGenerationAttempt, DBGenerationUsageEntry, DBRightsSnapshot,
    )
    state = json.loads(REVOKED_STATE.read_text())
    time.sleep(8)  # prove both running daemon loops have time to retry claim
    with SessionLocal() as db:
        task = db.get(DBGenerationTask, state["taskId"])
        assert task and task.project_id == state["projectId"]
        latest = db.execute(select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == state["sourceId"],
            DBRightsSnapshot.purpose == "REMIX",
        ).order_by(DBRightsSnapshot.captured_at.desc())).scalars().first()
        assert latest and latest.status == "REVOKED"
        def count(model, *predicates):
            return db.execute(select(func.count(model.id)).where(*predicates)).scalar_one()
        claimed = count(DBGenerationEvent,
            DBGenerationEvent.generation_task_id == task.id,
            DBGenerationEvent.event_type == "TASK_CLAIMED")
        attempts = count(DBGenerationAttempt,
            DBGenerationAttempt.generation_task_id == task.id)
        settled = count(DBGenerationUsageEntry,
            DBGenerationUsageEntry.task_id == task.id,
            DBGenerationUsageEntry.kind == "SETTLED")
        released = count(DBGenerationUsageEntry,
            DBGenerationUsageEntry.task_id == task.id,
            DBGenerationUsageEntry.kind == "RELEASED")
        reserved = count(DBGenerationUsageEntry,
            DBGenerationUsageEntry.task_id == task.id,
            DBGenerationUsageEntry.kind == "RESERVED")
        ingested = count(DBGenerationEvent,
            DBGenerationEvent.generation_task_id == task.id,
            DBGenerationEvent.event_type == "ARTIFACT_INGESTED")
        videos = count(DBAssetVersion,
            DBAssetVersion.project_id == task.project_id,
            DBAssetVersion.media_type == "video")
        assert task.status == "QUEUED" and task.lease_owner is None, (
            "Current pending-rights lifecycle changed: re-review Owner policies",
            task.status, task.lease_owner)
        assert (claimed, attempts, settled, released, reserved, ingested, videos) == (
            0, 1, 0, 0, 1, 0, 0), {
                "claimed": claimed, "attempts": attempts, "settled": settled,
                "released": released, "reserved": reserved,
                "ingested": ingested, "videos": videos}
    print(json.dumps({
        "gate": "G72_PG_TWO_LIVE_DAEMONS_PRECLAIM_REVOKE_FAIL_CLOSED",
        "result": "NO_CLAIM_NO_ARTIFACT_NO_SETTLEMENT",
        "status": "QUEUED", "lifecycle": "OPEN_STALE_QUEUED_AFTER_REVOKE",
        "rights": "SYNTHETIC_ONLY", "claimed": 0,
        "attempts": 1, "reserved": 1, "settled": 0,
        "released": 0, "ingested": 0, "videos": 0,
    }), flush=True)


def submit_revoke():
    submit(state_path=REVOKED_STATE)


def lock_revoke():
    lock(inject_revoke=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("submit", "lock", "verify",
                                         "submit-revoke", "lock-revoke",
                                         "verify-revoked"))
    args = parser.parse_args()
    guard()
    {"submit": submit, "lock": lock, "verify": verify,
     "submit-revoke": submit_revoke, "lock-revoke": lock_revoke,
     "verify-revoked": verify_revoked}[args.mode]()
