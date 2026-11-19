"""Disposable two-Docker-container POSIX JobStore race/crash smoke.

Synthetic isolated JobStore only: no render, Provider, protected record or credential.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def require_lab():
    if os.environ.get("AETHER_JOBSTORE_CI_LAB") != "synthetic-only":
        raise RuntimeError("An explicit synthetic-only CI gate is required")


def safe_root(s):
    root = Path(s)
    if (not root.is_absolute() or root.parent != Path("/media")
            or not root.name.startswith("g70g71-two-container-")):
        raise RuntimeError("Refusing unsafe non-ephemeral storage root")
    return root


def atomic_json(path, obj):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def claim(root, label):
    from app.main import JobStore
    evidence = root / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    store = JobStore(root)
    payload, execute = store.create_or_get(
        "synthetic-tenant-a", "render", "synthetic-shared-request",
        request_hash="a" * 64,
    )
    atomic_json(evidence / (label + ".json"), {
        "label": label, "shouldExecute": execute,
        "status": payload["status"], "pid": os.getpid(),
        "jobId": payload["jobId"],
    })
    deadline = time.monotonic() + 75
    while not (evidence / "release").is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("Synthetic two-container barrier expired")
        time.sleep(0.05)


def probe(root):
    from app.main import JobStore
    path = root / "jobs" / "synthetic-shared-request.json"
    before = path.read_bytes()
    store = JobStore(root)
    for project, digest in [
        ("synthetic-tenant-b", "a" * 64),
        ("synthetic-tenant-a", "b" * 64),
    ]:
        try:
            store.create_or_get(
                project, "render", "synthetic-shared-request",
                request_hash=digest,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Tenant/different-input claim was accepted")
        assert path.read_bytes() == before, "Rejected claim mutated owner's bytes"
    job = store.get("synthetic-shared-request")
    assert job and job["status"] == "queued", (
        "Live owner in another PID namespace falsely marked dead", job,
    )
    print("TWO_CONTAINER_LIVE_OWNER_TENANT_FENCE_PASS", flush=True)


def verify_after_crash(root):
    from app.main import JobStore
    store = JobStore(root)
    job = store.get("synthetic-shared-request")
    assert job and job["status"] == "failed", job
    assert job["retryable"] is False and "UNKNOWN" in job["message"], job
    repeated, execute = store.create_or_get(
        "synthetic-tenant-a", "render", "synthetic-shared-request",
        request_hash="a" * 64,
    )
    assert not execute and repeated["status"] == "failed", repeated
    assert not (root / "jobs" / "synthetic-shared-request.json.pending").exists()
    print("TWO_CONTAINER_SIGKILL_UNKNOWN_FENCE_PASS", flush=True)


def checked(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def orchestrate():
    require_lab()
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or not os.environ.get("COMPOSE_PROJECT_NAME", "").startswith("aether-g72-")):
        raise RuntimeError("Only allowed in disposable GitHub FAKE-only Compose")
    compose = [
        "docker", "compose", "-f", "infra/docker/docker-compose.yml",
        "-f", "infra/docker/docker-compose.g72-fake-only.override.yml",
    ]
    primary = checked(compose + ["ps", "-q", "video-use"], capture_output=True).stdout.strip()
    assert primary, "Primary Sidecar not found"
    run_id = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    peer = "g70g71-ci-peer-" + run_id
    root = "/media/g70g71-two-container-" + run_id
    script = "/tmp/g70g71-two-container-smoke.py"
    processes = []
    try:
        checked(compose + ["run", "-d", "--no-deps", "--name", peer, "video-use"],
                stdout=subprocess.DEVNULL)
        for container in (primary, peer):
            checked(["docker", "cp", __file__, container + ":" + script],
                    stdout=subprocess.DEVNULL)
        env = ["-e", "AETHER_JOBSTORE_CI_LAB=synthetic-only"]
        checked(["docker", "exec", *env, primary, "python", script, "mark", root])
        checked(["docker", "exec", *env, peer, "python", script, "see", root])
        for label, container in [("primary", primary), ("peer", peer)]:
            processes.append(subprocess.Popen([
                "docker", "exec", *env, container, "python", "-u", script,
                "claim", root, label,
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        deadline = time.monotonic() + 65
        while True:
            result = checked(
                ["docker", "exec", *env, primary, "python", script, "status", root],
                capture_output=True,
            )
            if result.stdout.strip() == "ready":
                break
            for i, candidate in enumerate(processes):
                if candidate.poll() is not None:
                    output = candidate.communicate(timeout=5)[0]
                    raise RuntimeError(
                        f"Claimant {i} exited early with code {candidate.returncode}: "
                        f"{output[-6000:]}"
                    )
            if time.monotonic() >= deadline:
                raise TimeoutError("Two-container claim barrier not reached")
            time.sleep(0.15)
        checked(["docker", "exec", *env, peer, "python", script, "probe", root])
        records = json.loads(checked(
            ["docker", "exec", *env, peer, "python", script, "results", root],
            capture_output=True,
        ).stdout)
        assert len(records) == 2 and sum(r["shouldExecute"] for r in records) == 1, records
        owner = next(r for r in records if r["shouldExecute"])
        owner_container = primary if owner["label"] == "primary" else peer
        checked([
            "docker", "exec", owner_container, "python", "-c",
            "import os,signal,sys;os.kill(int(sys.argv[1]),signal.SIGKILL)",
            str(owner["pid"]),
        ])
        checked(["docker", "exec", *env, peer, "python", script, "release", root])
        for p in processes:
            p.communicate(timeout=15)
        checked(["docker", "exec", *env, peer, "python", script, "verify", root])
        print("TWO_CONTAINER_SHARED_VOLUME_FAKE_ONLY_PASS", flush=True)
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=5)
        subprocess.run(["docker", "rm", "-f", peer],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # CI's run-scoped docker compose down --volumes removes synthetic data.


def subcommand(command, root, label=None):
    require_lab()
    evidence = root / "evidence"
    if command == "mark":
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "shared-volume-marker").write_text("fake-only")
    elif command == "see":
        assert (evidence / "shared-volume-marker").read_text() == "fake-only"
    elif command == "claim":
        assert label in {"primary", "peer"}
        claim(root, label)
    elif command == "status":
        print("ready" if all((evidence / (x + ".json")).is_file()
                             for x in ("primary", "peer")) else "pending")
    elif command == "probe":
        probe(root)
    elif command == "results":
        print(json.dumps([
            json.loads((evidence / (x + ".json")).read_text())
            for x in ("primary", "peer")
        ]))
    elif command == "release":
        (evidence / "release").write_text("released")
    elif command == "verify":
        verify_after_crash(root)
    else:
        raise ValueError("Unrecognized action")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "orchestrate":
        orchestrate()
    elif len(sys.argv) in (3, 4):
        subcommand(sys.argv[1], safe_root(sys.argv[2]),
                   sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        raise SystemExit("usage: script.py orchestrate | action root [label]")
