"""Disposable G72 PG table-lock race gate. FAKE-ONLY, synthetic rights.

Execute in the disposable PostgreSQL API container. This is not trusted M12
issuance, delegated-Owner acceptance, or production evidence.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import time
import uuid
from sqlalchemy import text

STATE = Path("/tmp/g72-pg-native-worker-lock-race.json")
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


def submit():
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
    assert not STATE.exists(), "state must be disposable and unique"
    STATE.write_text(json.dumps({"projectId": pid, "taskId": task_id}))
    print("G72_PG_LOCK_RACE_TASK_QUEUED", flush=True)


def lock():
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("submit", "lock", "verify"))
    args = parser.parse_args()
    guard()
    {"submit": submit, "lock": lock, "verify": verify}[args.mode]()
