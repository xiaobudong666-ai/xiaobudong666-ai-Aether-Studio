"""Native FastAPI+disk contract tests for G70 FAKE-only recovery.

These use synthetic MP4 bytes and a stub probe, NOT validated ffmpeg media,
production entitlements, paid generation, PostgreSQL or cross-project trust.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as sidecar


PAYLOAD = b"synthetic-disposable-mp4-for-g70-recovery" * 64
SHA = hashlib.sha256(PAYLOAD).hexdigest()
KEY = "g70-attempt-001"


@pytest.fixture
def fake_probe(monkeypatch):
    monkeypatch.setattr(
        sidecar, "probe",
        lambda path: {
            "durationSeconds": 1.25,
            "sizeBytes": path.stat().st_size,
            "video": {"codec": "h264", "width": 320, "height": 240},
            "audio": None,
        },
    )


def upload(client, project="proj_a", key=KEY, expected=SHA, payload=PAYLOAD):
    return client.post(
        "/media",
        data={"projectId": project, "recoveryKey": key, "expectedSha256": expected},
        files={"file": ("generation.mp4", payload, "video/mp4")},
    )


def recovery(client, project="proj_a", key=KEY, expected=SHA):
    return client.get(
        f"/fake-recovery/{project}/{key}",
        params={"expectedSha256": expected},
    )


@pytest.mark.parametrize(
    "fake,stage",
    [(None, None), ("true", None), (None, "nonproduction"), ("true", "production")],
)
def test_g70_recovery_fails_closed_without_both_fake_gates(
    tmp_path, monkeypatch, fake_probe, fake, stage,
):
    for name, value in (
        ("AETHER_FAKE_ONLY_NONPROD", fake),
        ("AETHER_RUNTIME_STAGE", stage),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with TestClient(sidecar.create_app(media_root=tmp_path / "media")) as client:
        assert upload(client).status_code == 403
        assert recovery(client).status_code == 403
    assert not list((tmp_path / "media").rglob("g70*.mp4"))


def test_g70_ack_lost_media_readback_survives_new_service_instance(
    tmp_path, monkeypatch, fake_probe,
):
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", "true")
    monkeypatch.setenv("AETHER_RUNTIME_STAGE", "nonproduction")
    root = tmp_path / "media"
    with TestClient(sidecar.create_app(media_root=root)) as client:
        created = upload(client)
        assert created.status_code == 201, created.text
        first = created.json()
        assert first["sha256"] == SHA
        assert first["projectId"] == "proj_a"
        assert first["recovered"] is False
        assert first["mediaId"].startswith("g70")
        assert recovery(client).json()["mediaId"] == first["mediaId"]

    # Simulates a lost HTTP ACK + sidecar restart: new FastAPI process model
    # reads the original durable bytes, not a second upload/generated asset.
    with TestClient(sidecar.create_app(media_root=root)) as restarted:
        readback = recovery(restarted)
        assert readback.status_code == 200, readback.text
        assert readback.json()["mediaId"] == first["mediaId"]
        assert readback.json()["recovered"] is True
        assert readback.json()["sha256"] == SHA
        assert restarted.get(f"/media/proj_a/{first['mediaId']}").content == PAYLOAD
    stored = list(root.rglob("g70*.mp4"))
    assert len(stored) == 1
    assert hashlib.sha256(stored[0].read_bytes()).hexdigest() == SHA


def test_g70_duplicate_upload_keeps_same_physical_media(
    tmp_path, monkeypatch, fake_probe,
):
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", "true")
    monkeypatch.setenv("AETHER_RUNTIME_STAGE", "nonproduction")
    root = tmp_path / "media"
    with TestClient(sidecar.create_app(media_root=root)) as client:
        first = upload(client)
        duplicate = upload(client)
        assert first.status_code == duplicate.status_code == 201
        assert first.json()["mediaId"] == duplicate.json()["mediaId"]
        assert duplicate.json()["recovered"] is True
        assert list(root.rglob("g70*.mp4")) and len(list(root.rglob("g70*.mp4"))) == 1


def test_g70_wrong_digest_never_overwrites_original(
    tmp_path, monkeypatch, fake_probe,
):
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", "true")
    monkeypatch.setenv("AETHER_RUNTIME_STAGE", "nonproduction")
    root = tmp_path / "media"
    with TestClient(sidecar.create_app(media_root=root)) as client:
        original = upload(client)
        assert original.status_code == 201
        wrong = hashlib.sha256(b"not-the-original").hexdigest()
        assert upload(client, expected=wrong).status_code == 409
        assert recovery(client, expected=wrong).status_code == 409
        assert recovery(client).status_code == 200
    stored = list(root.rglob("g70*.mp4"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == PAYLOAD


def test_g70_tenant_project_scope_and_invalid_requests_fail_closed(
    tmp_path, monkeypatch, fake_probe,
):
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", "true")
    monkeypatch.setenv("AETHER_RUNTIME_STAGE", "nonproduction")
    root = tmp_path / "media"
    with TestClient(sidecar.create_app(media_root=root)) as client:
        assert upload(client).status_code == 201
        assert recovery(client, project="proj_b").status_code == 404
        assert recovery(client, key="other_attempt").status_code == 404
        assert recovery(client, expected="invalid").status_code == 422
        assert upload(client, expected="invalid").status_code == 422
        assert upload(client, project="../escape").status_code == 422
        incomplete = client.post(
            "/media",
            data={"projectId": "proj_a", "recoveryKey": KEY},
            files={"file": ("generation.mp4", PAYLOAD, "video/mp4")},
        )
        assert incomplete.status_code == 422


def test_g70_mismatched_upload_has_no_finalized_physical_media(
    tmp_path, monkeypatch, fake_probe,
):
    monkeypatch.setenv("AETHER_FAKE_ONLY_NONPROD", "true")
    monkeypatch.setenv("AETHER_RUNTIME_STAGE", "nonproduction")
    root = tmp_path / "media"
    with TestClient(sidecar.create_app(media_root=root)) as client:
        response = upload(client, payload=b"unexpected-media-bytes")
        assert response.status_code == 409
        assert recovery(client).status_code == 404
        assert not list(root.rglob("g70*.mp4"))
