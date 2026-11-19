import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from app.main import UPSTREAM_COMMIT, create_app
from fastapi.testclient import TestClient


def make_source(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def make_image(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=48x48",
            "-frames:v",
            "1",
            "-update",
            "1",
            str(path),
        ],
        check=True,
    )


def make_fake_upstream(root: Path) -> None:
    helpers = root / "helpers"
    helpers.mkdir(parents=True)
    (helpers / "render.py").write_text(
        """
import argparse
import json
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("edl")
parser.add_argument("-o", "--output", required=True)
parser.add_argument("--preview", action="store_true")
parser.add_argument("--draft", action="store_true")
parser.add_argument("--no-subtitles", action="store_true")
parser.add_argument("--no-loudnorm", action="store_true")
args = parser.parse_args()
edl = json.loads(Path(args.edl).read_text())
source = Path(edl["sources"][edl["ranges"][0]["source"]])
shutil.copyfile(source, args.output)
""".strip()
    )
    (helpers / "transcribe.py").write_text("print('transcribe helper')")
    (helpers / "timeline_view.py").write_text("print('timeline helper')")


def wait_for_job(client: TestClient, job_id: str) -> dict:
    for _ in range(100):
        payload = client.get(f"/jobs/{job_id}").json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not complete")


def test_upload_probe_render_and_download_are_real(tmp_path):
    upstream = tmp_path / "upstream"
    make_fake_upstream(upstream)
    source = tmp_path / "source.mp4"
    make_source(source)

    with TestClient(create_app(media_root=tmp_path / "media", upstream_root=upstream)) as client:
        health = client.get("/health").json()
        assert health["status"] == "healthy"
        capabilities = client.get("/capabilities").json()
        assert capabilities["commit"] == UPSTREAM_COMMIT
        assert capabilities["transcription"]["configured"] is False

        with source.open("rb") as stream:
            upload = client.post(
                "/media",
                data={"projectId": "project-1"},
                files={"file": ("source.mp4", stream, "video/mp4")},
            )
        assert upload.status_code == 201
        media = upload.json()
        assert media["metadata"]["video"]["width"] == 320
        assert media["metadata"]["durationSeconds"] > 0.9
        inspected = client.get(f"/media/project-1/{media['mediaId']}/inspect")
        assert inspected.status_code == 200, inspected.text
        inspection = inspected.json()
        assert inspection["projectId"] == "project-1"
        assert inspection["mediaId"] == media["mediaId"]
        assert inspection["contentType"] == "video/mp4"
        assert inspection["sizeBytes"] == len(source.read_bytes())
        assert inspection["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert inspection["metadata"]["video"]["width"] == 320

        render = client.post(
            "/renders",
            json={
                "projectId": "project-1",
                "ranges": [{"mediaId": media["mediaId"], "start": 0, "end": 0.8}],
                "mode": "preview",
                "grade": "subtle",
            },
        )
        assert render.status_code == 202
        job = wait_for_job(client, render.json()["jobId"])
        assert job["status"] == "completed"
        assert job["metadata"]["video"]["codec"] == "h264"

        artifact = client.get(f"/jobs/{job['jobId']}/artifact")
        assert artifact.status_code == 200
        assert artifact.headers["content-type"] == "video/mp4"
        assert len(artifact.content) > 1_000


def test_generated_upload_idempotency_survives_lost_ack_without_duplicate_media(tmp_path):
    upstream = tmp_path / "upstream"
    make_fake_upstream(upstream)
    source = tmp_path / "source.mp4"
    make_source(source)
    image = tmp_path / "different.png"
    make_image(image)
    media_root = tmp_path / "media"
    idempotency_key = "generation:task-idempotency-test"
    expected_media_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"aether-video-use:project-idempotency:{idempotency_key}",
    ))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    with TestClient(create_app(media_root=media_root, upstream_root=upstream)) as client:
        headers = {
            "Idempotency-Key": idempotency_key,
            "X-Content-SHA256": digest,
        }
        with source.open("rb") as stream:
            first = client.post(
                "/media",
                data={"projectId": "project-idempotency"},
                files={"file": ("generated.mp4", stream, "video/mp4")},
                headers=headers,
            )
        assert first.status_code == 201, first.text
        first_payload = first.json()
        assert first_payload["mediaId"] == expected_media_id
        assert first_payload["idempotentReplay"] is False

        with source.open("rb") as stream:
            replay = client.post(
                "/media",
                data={"projectId": "project-idempotency"},
                files={"file": ("generated.mp4", stream, "video/mp4")},
                headers=headers,
            )
        assert replay.status_code == 201, replay.text
        replay_payload = replay.json()
        assert replay_payload["mediaId"] == expected_media_id
        assert replay_payload["idempotentReplay"] is True
        assert first_payload["fileName"] == "generation-task-idempotency-test.mp4"
        assert replay_payload["fileName"] == first_payload["fileName"]
        assert replay_payload["contentType"] == first_payload["contentType"] == "video/mp4"

        # Same durable bytes/suffix and idempotency key, but a different
        # multipart filename/MIME must not forge a different Sidecar receipt.
        with source.open("rb") as stream:
            replay_with_untrusted_metadata = client.post(
                "/media",
                data={"projectId": "project-idempotency"},
                files={"file": ("renamed.mp4", stream, "application/octet-stream")},
                headers=headers,
            )
        assert replay_with_untrusted_metadata.status_code == 201, replay_with_untrusted_metadata.text
        replay_receipt = replay_with_untrusted_metadata.json()
        assert replay_receipt["idempotentReplay"] is True
        assert replay_receipt["mediaId"] == first_payload["mediaId"]
        assert replay_receipt["fileName"] == first_payload["fileName"]
        assert replay_receipt["contentType"] == first_payload["contentType"]

        # G70/G71: a lost-ACK replay must preserve not only the byte hash,
        # but also the original suffix/format binding. The same MP4 bytes
        # labeled MOV must be rejected before the Sidecar can return an
        # inconsistent media filename or format receipt.
        with source.open("rb") as stream:
            cross_suffix = client.post(
                "/media",
                data={"projectId": "project-idempotency"},
                files={"file": ("generated.mov", stream, "video/quicktime")},
                headers=headers,
            )
        assert cross_suffix.status_code == 409, cross_suffix.text
        assert "different media parameters" in cross_suffix.json()["detail"]

        media_files = list(
            (media_root / "projects" / "project-idempotency" / "media").glob(
                f"{expected_media_id}.*"
            )
        )
        assert len(media_files) == 1

        conflicting_headers = {
            "Idempotency-Key": idempotency_key,
            "X-Content-SHA256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }
        with image.open("rb") as stream:
            conflict = client.post(
                "/media",
                data={"projectId": "project-idempotency"},
                files={"file": ("different.png", stream, "image/png")},
                headers=conflicting_headers,
            )
        assert conflict.status_code == 409, conflict.text
        media_files_after_conflict = list(
            (media_root / "projects" / "project-idempotency" / "media").glob(
                f"{expected_media_id}.*"
            )
        )
        assert len(media_files_after_conflict) == 1
        assert hashlib.sha256(media_files_after_conflict[0].read_bytes()).hexdigest() == digest


def test_upload_probe_image_is_classified_and_stored_as_image(tmp_path):
    upstream = tmp_path / "upstream"
    make_fake_upstream(upstream)
    image = tmp_path / "portrait.png"
    make_image(image)

    with TestClient(create_app(media_root=tmp_path / "media", upstream_root=upstream)) as client:
        with image.open("rb") as stream:
            upload = client.post(
                "/media",
                data={"projectId": "image-project"},
                files={"file": ("portrait.png", stream, "image/png")},
            )
        assert upload.status_code == 201, upload.text
        payload = upload.json()
        assert payload["mediaType"] == "image"
        assert payload["metadata"]["video"]["width"] == 48
        assert payload["metadata"]["video"]["height"] == 48
        assert payload["metadata"]["audio"] is None
        stored = client.get(f"/media/image-project/{payload['mediaId']}")
        assert stored.status_code == 200
        assert stored.content == image.read_bytes()


def test_render_rejects_unknown_media_and_unsafe_identifiers(tmp_path):
    upstream = tmp_path / "upstream"
    make_fake_upstream(upstream)
    with TestClient(create_app(media_root=tmp_path / "media", upstream_root=upstream)) as client:
        missing = client.post(
            "/renders",
            json={
                "projectId": "project-1",
                "ranges": [
                    {
                        "mediaId": "00000000-0000-0000-0000-000000000000",
                        "start": 0,
                        "end": 1,
                    }
                ],
            },
        )
        assert missing.status_code == 404

        unsafe = client.post(
            "/renders",
            json={
                "projectId": "../escape",
                "ranges": [{"mediaId": "media-1", "start": 0, "end": 1}],
            },
        )
        assert unsafe.status_code == 422


def test_canonical_timeline_render_preserves_gap_overlap_audio_and_idempotency(tmp_path):
    upstream = tmp_path / "upstream"
    make_fake_upstream(upstream)
    source = tmp_path / "source.mp4"
    make_source(source)

    with TestClient(create_app(media_root=tmp_path / "media", upstream_root=upstream)) as client:
        with source.open("rb") as stream:
            media = client.post(
                "/media",
                data={"projectId": "canonical-project"},
                files={"file": ("source.mp4", stream, "video/mp4")},
            ).json()
        clip = {
            "materialId": media["mediaId"],
            "sourceIn": {"value": 0, "timescale": 24},
            "duration": {"value": 24, "timescale": 24},
        }
        payload = {
            "projectId": "canonical-project",
            "requestId": "canonical-task-1",
            "canonicalTimeline": {
                "version": "1.1",
                "duration": {"value": 4, "timescale": 1},
                "output": {
                    "width": 320,
                    "height": 240,
                    "fps": {"value": 24, "timescale": 1},
                    "backgroundColor": "black",
                },
                "materials": [{"id": media["mediaId"], "type": "video"}],
                "tracks": [
                    {
                        "id": "video-main", "type": "video", "order": 0,
                        "clips": [
                            {**clip, "id": "first", "start": {"value": 0, "timescale": 24}},
                            {**clip, "id": "last", "start": {"value": 72, "timescale": 24}},
                        ],
                    },
                    {
                        "id": "video-overlay", "type": "video", "order": 1,
                        "clips": [
                            {
                                **clip, "id": "overlay", "start": {"value": 12, "timescale": 24},
                                "duration": {"value": 12, "timescale": 24},
                                "width": 160, "height": 120, "x": 8, "y": 8, "opacity": 0.7,
                            }
                        ],
                    },
                    {
                        "id": "audio-bed", "type": "audio", "order": 2,
                        "clips": [
                            {**clip, "id": "audio", "start": {"value": 48, "timescale": 24}, "volume": 0.4}
                        ],
                    },
                    {
                        "id": "subtitles", "type": "subtitle", "order": 3,
                        "clips": [
                            {
                                **clip, "id": "subtitle", "start": {"value": 0, "timescale": 24},
                                "duration": {"value": 12, "timescale": 24},
                                "text": "Aether subtitle",
                            }
                        ],
                    },
                ],
            },
        }
        render = client.post("/renders", json=payload)
        assert render.status_code == 202
        duplicate = client.post("/renders", json=payload)
        assert duplicate.status_code == 202
        assert duplicate.json()["jobId"] == render.json()["jobId"]
        job = wait_for_job(client, render.json()["jobId"])
        assert job["status"] == "completed", job
        assert job["metadata"]["durationSeconds"] == pytest.approx(4.0, abs=0.15)
        artifact = client.get(f"/jobs/{job['jobId']}/artifact")
        assert artifact.status_code == 200
        assert len(artifact.content) > 1_000
        rendered_path = tmp_path / "canonical-render.mp4"
        rendered_path.write_bytes(artifact.content)
        gap_pixel = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "2",
                "-i", str(rendered_path), "-vf", "scale=1:1", "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            check=True,
            capture_output=True,
        ).stdout
        assert gap_pixel and max(gap_pixel) < 20


def test_idempotent_upload_conflict_is_serialized_across_independent_apps(tmp_path, monkeypatch):
    # Two separate ASGI app instances with the same POSIX media directory
    # mimic independent workers; this is not a PostgreSQL or host-crash test.
    source = tmp_path / "original.mp4"
    alternate = tmp_path / "alternate.mp4"
    make_source(source)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:r=24",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(alternate),
        ],
        check=True,
    )
    assert source.read_bytes() != alternate.read_bytes()

    media_root = tmp_path / "media"
    app_a = create_app(media_root=media_root, upstream_root=tmp_path / "upstream")
    app_b = create_app(media_root=media_root, upstream_root=tmp_path / "upstream")
    original_replace = Path.replace
    replace_barrier = threading.Barrier(2)

    def synchronized_replace(self, target):
        # Force an overlapping final-write window in the old implementation.
        # Under the fix the first caller times out here and the second
        # observes an already committed media file before trying to replace.
        if self.name.endswith(".pending.mp4") and "project-shared" in self.parts:
            try:
                replace_barrier.wait(timeout=1.2)
            except threading.BrokenBarrierError:
                pass
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", synchronized_replace)

    def upload(client, source_path):
        payload = source_path.read_bytes()
        with source_path.open("rb") as content:
            return client.post(
                "/media",
                data={"projectId": "project-shared"},
                files={"file": ("generated.mp4", content, "video/mp4")},
                headers={
                    "Idempotency-Key": "shared-key",
                    "X-Content-SHA256": hashlib.sha256(payload).hexdigest(),
                },
            )

    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(upload, client_a, source)
            future_b = pool.submit(upload, client_b, alternate)
            response_a = future_a.result(timeout=30)
            response_b = future_b.result(timeout=30)

    assert sorted([response_a.status_code, response_b.status_code]) == [201, 409], (
        response_a.text, response_b.text
    )
    media_files = list(
        (media_root / "projects" / "project-shared" / "media").glob("*.mp4")
    )
    assert len(media_files) == 1
    winner = source if response_a.status_code == 201 else alternate
    assert media_files[0].read_bytes() == winner.read_bytes()

# G70/G71: independently started OS processes sharing one POSIX media directory.
# FAKE-ONLY, localhost network only; not a multi-host/PG/Owner ACK release proof.
def test_shared_posix_media_root_two_live_uvicorn_processes(tmp_path):
    import os
    import socket
    import sys
    import httpx

    def make_clip(path, color):
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24",
            "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ], check=True)

    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    make_clip(first, "red")
    make_clip(second, "blue")
    assert first.read_bytes() != second.read_bytes()
    media_root = tmp_path / "shared-media"
    ports = []
    for _ in range(2):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        assert port not in ports
        ports.append(port)
    env = {
        **os.environ,
        "VIDEO_USE_MEDIA_ROOT": str(media_root),
        "VIDEO_USE_UPSTREAM_ROOT": str(tmp_path / "upstream"),
        "VIDEO_USE_WORKERS": "1",
    }
    processes = []
    try:
        for port in ports:
            process = subprocess.Popen([
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--log-level", "error", "--no-access-log",
            ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(process)
        for port, process in zip(ports, processes):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, "Uvicorn child unexpectedly exited"
                try:
                    if httpx.get(
                        f"http://127.0.0.1:{port}/openapi.json", timeout=0.5
                    ).status_code == 200:
                        break
                except httpx.RequestError:
                    time.sleep(0.1)
            else:
                raise AssertionError("Uvicorn process did not become ready")

        def upload(port, idem_key, path):
            body = path.read_bytes()
            return httpx.post(
                f"http://127.0.0.1:{port}/media",
                data={"projectId": "ci-shared-posix"},
                files={"file": ("generated.mp4", body, "video/mp4")},
                headers={
                    "Idempotency-Key": idem_key,
                    "X-Content-SHA256": hashlib.sha256(body).hexdigest(),
                },
                timeout=25,
            )

        for i in range(3):
            idem_key = f"cross-process-same-key-{i}"
            with ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(upload, ports[0], idem_key, first)
                b = executor.submit(upload, ports[1], idem_key, second)
                response_a, response_b = a.result(timeout=35), b.result(timeout=35)
            assert sorted([response_a.status_code, response_b.status_code]) == [201, 409], (
                response_a.text, response_b.text
            )
            winner = first if response_a.status_code == 201 else second
            winning_response = response_a if response_a.status_code == 201 else response_b
            media_id = winning_response.json()["mediaId"]
            stored = list(
                (media_root / "projects" / "ci-shared-posix" / "media").glob(
                    f"{media_id}.*"
                )
            )
            assert len(stored) == 1
            assert stored[0].read_bytes() == winner.read_bytes()
            loser_port = ports[1] if response_a.status_code == 201 else ports[0]
            replay = upload(loser_port, idem_key, winner)
            assert replay.status_code == 201, replay.text
            assert replay.json()["idempotentReplay"] is True
            assert stored[0].read_bytes() == winner.read_bytes()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


def test_render_executor_submission_rejection_fails_closed(tmp_path):
    """Synthetic failure: durable queued job must not be presented as running."""
    import json

    class RejectingExecutor:
        calls = 0

        def submit(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("synthetic rejected hand-off")

    pool = RejectingExecutor()
    media_root = tmp_path / "media"
    payload = {
        "projectId": "fake-project-a",
        "requestId": "fake-render-submit-failure",
        "canonicalTimeline": {
            "version": "1.1",
            "duration": {"value": 1, "timescale": 1},
            "output": {
                "width": 1280,
                "height": 720,
                "fps": {"value": 24, "timescale": 1},
                "backgroundColor": "black",
            },
            "materials": [],
            "tracks": [
                {"id": "subtitles", "type": "subtitle", "order": 0, "clips": []}
            ],
        },
    }
    app = create_app(
        media_root=media_root,
        upstream_root=tmp_path / "upstream",
        executor=pool,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        failure = client.post("/renders", json=payload)
        assert failure.status_code == 409, failure.text
        first = client.get("/jobs/fake-render-submit-failure")
        assert first.status_code == 200
        assert first.json()["status"] == "failed"
        assert first.json()["retryable"] is False
        assert "UNKNOWN" in first.json()["message"]
        replay = client.post("/renders", json=payload)
        assert replay.status_code == 202
        assert replay.json()["status"] == "failed"
        assert pool.calls == 1
        changed_project = dict(payload, projectId="fake-project-b")
        denied = client.post("/renders", json=changed_project)
        assert denied.status_code == 409
        assert pool.calls == 1
    persisted = json.loads(
        (media_root / "jobs" / "fake-render-submit-failure.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["status"] == "failed"
    assert persisted["retryable"] is False
    assert persisted["artifact"] is None


@pytest.mark.parametrize(
    ("endpoint", "payload", "kind"),
    [
        ("/transcriptions", {"projectId": "fake-project-a", "mediaId": "fake-input", "language": "en"}, "transcription"),
        ("/timeline-views", {"projectId": "fake-project-a", "mediaId": "fake-input", "start": 0, "end": 1}, "timeline-view"),
    ],
)
def test_auxiliary_executor_submission_rejection_fails_closed(
    tmp_path, monkeypatch, endpoint, payload, kind
):
    """FAKE-only: a refused Sidecar hand-off cannot leave a durable QUEUED job."""
    import json

    from app.main import MediaStore

    class RejectingExecutor:
        def __init__(self):
            self.calls = 0

        def submit(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("synthetic auxiliary executor refusal")

    # No real media, helper, generation Provider, network or billable work.
    monkeypatch.setattr(
        MediaStore,
        "find_media",
        lambda _self, _project_id, _media_id: tmp_path / "synthetic.mp4",
    )
    pool = RejectingExecutor()
    media_root = tmp_path / "media"
    app = create_app(media_root=media_root, upstream_root=tmp_path / "upstream", executor=pool)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(endpoint, json=payload)
        assert response.status_code == 409, response.text
        assert "UNKNOWN" in response.text
        assert pool.calls == 1
        job_files = list((media_root / "jobs").glob("*.json"))
        assert len(job_files) == 1
        persisted = json.loads(job_files[0].read_text(encoding="utf-8"))
        assert persisted["kind"] == kind
        assert persisted["projectId"] == payload["projectId"]
        assert persisted["status"] == "failed"
        assert persisted["retryable"] is False
        assert "UNKNOWN" in persisted["message"]
        assert persisted["artifact"] is None
        lookup = client.get("/jobs/" + persisted["jobId"])
        assert lookup.status_code == 200
        assert lookup.json()["status"] == "failed"
        assert lookup.json()["retryable"] is False


@pytest.mark.parametrize("stalled_status", ["queued", "processing"])
def test_jobstore_restart_unknown_requires_manual_reconciliation(tmp_path, stalled_status):
    """FAKE-only crash: restart must not silently re-run a possibly accepted job."""
    from app.main import JobStore

    media_root = tmp_path / "fake-media"
    original = JobStore(media_root)
    first, should_execute = original.create_or_get(
        "fake-project-a", "render", "fake-unknown-restart"
    )
    assert should_execute is True
    if stalled_status == "processing":
        original.update(first["jobId"], status="processing", progress=30)

    restarted = JobStore(media_root)
    held = restarted.get(first["jobId"])
    assert held["status"] == "failed"
    assert held["retryable"] is False
    assert "UNKNOWN" in held["message"]
    assert held["projectId"] == "fake-project-a"
    replay, should_execute = restarted.create_or_get(
        "fake-project-a", "render", first["jobId"]
    )
    assert should_execute is False
    assert replay["status"] == "failed"
    with pytest.raises(ValueError, match="another job"):
        restarted.create_or_get("fake-project-b", "render", first["jobId"])
    persisted = JobStore(media_root).get(first["jobId"])
    assert persisted["retryable"] is False


def test_jobstore_legacy_restart_retry_marker_fails_closed(tmp_path):
    """Prior durable restart marker must not authorize automatic replay."""
    from app.main import JobStore

    media_root = tmp_path / "fake-media"
    original = JobStore(media_root)
    first, _ = original.create_or_get("fake-project-a", "render", "legacy-restart")
    original.update(
        first["jobId"],
        status="failed",
        progress=100,
        retryable=True,
        message="渲染服务重启导致任务中断，可以安全重试",
    )
    restarted = JobStore(media_root)
    held = restarted.get(first["jobId"])
    assert held["status"] == "failed"
    assert held["retryable"] is False
    assert "UNKNOWN" in held["message"]
    replay, should_execute = restarted.create_or_get(
        "fake-project-a", "render", first["jobId"]
    )
    assert should_execute is False
    assert replay["status"] == "failed"


def test_render_same_request_id_changed_payload_fails_closed_before_redispatch(tmp_path):
    """FAKE-only: requestId must identify one exact render contract, even after restart."""
    import json

    class RejectingExecutor:
        def __init__(self):
            self.calls = 0

        def submit(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("synthetic handoff refusal")

    payload = {
        "projectId": "fake-fingerprint-project",
        "requestId": "fake-fingerprint-render",
        "canonicalTimeline": {
            "version": "1.1",
            "duration": {"value": 1, "timescale": 1},
            "output": {
                "width": 1280,
                "height": 720,
                "fps": {"value": 24, "timescale": 1},
                "backgroundColor": "black",
            },
            "materials": [],
            "tracks": [
                {"id": "subtitle", "type": "subtitle", "order": 0, "clips": []}
            ],
        },
    }
    media_root = tmp_path / "media"
    pool = RejectingExecutor()
    app = create_app(media_root=media_root, upstream_root=tmp_path / "upstream", executor=pool)
    with TestClient(app, raise_server_exceptions=False) as client:
        first = client.post("/renders", json=payload)
        assert first.status_code == 409, first.text
        same = client.post("/renders", json=payload)
        assert same.status_code == 202, same.text
        assert same.json()["status"] == "failed"
        changed_timeline = json.loads(json.dumps(payload))
        changed_timeline["canonicalTimeline"]["duration"]["value"] = 2
        assert client.post("/renders", json=changed_timeline).status_code == 409
        changed_mode = dict(payload, mode="final")
        assert client.post("/renders", json=changed_mode).status_code == 409
        assert pool.calls == 1

    restarted = create_app(media_root=media_root, upstream_root=tmp_path / "upstream", executor=pool)
    with TestClient(restarted, raise_server_exceptions=False) as client:
        assert client.post("/renders", json=payload).status_code == 202
        assert client.post("/renders", json=changed_timeline).status_code == 409
        assert pool.calls == 1


def test_render_request_id_two_live_uvicorn_processes_fail_closed_on_owner_exit(tmp_path):
    """FAKE-only shared POSIX root: exactly one dispatch, then UNKNOWN after owner exit.

    No FFmpeg, helper, Provider, paid API, credentials, user assets or production traffic.
    Both servers are independent localhost OS processes with NOOP executors.
    """
    import json
    import os
    import socket
    import sys
    import httpx

    root = tmp_path / "shared-persistent-jobs"
    dispatch_log = tmp_path / "fake-dispatches.jsonl"
    launch_script = '''
import json, os, sys
from concurrent.futures import Future
from pathlib import Path
import uvicorn
from app.main import create_app

root, output_log, port = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
class FakeNoopExecutor:
    def submit(self, _handler, job, _request):
        record = {"pid": os.getpid(), "jobId": job["jobId"], "FAKE_ONLY": True}
        fd = os.open(output_log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        future = Future()
        future.set_result(None)
        return future
app = create_app(media_root=root, upstream_root=root / "synthetic-upstream", executor=FakeNoopExecutor())
uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="error")
'''
    ports = []
    for _ in range(2):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ports.append(listener.getsockname()[1])
    assert ports[0] != ports[1]
    env = {
        **os.environ,
        "VIDEO_USE_MEDIA_ROOT": str(tmp_path / "never-used-global-app"),
        "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
        "http_proxy": "", "https_proxy": "", "all_proxy": "",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    processes = []
    try:
        for port in ports:
            processes.append(subprocess.Popen(
                [sys.executable, "-u", "-c", launch_script, str(root), str(dispatch_log), str(port)],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        with httpx.Client(timeout=5, trust_env=False) as client:
            for port, process in zip(ports, processes):
                deadline = time.monotonic() + 20
                while True:
                    assert process.poll() is None, "Synthetic Uvicorn child exited"
                    try:
                        if client.get(f"http://127.0.0.1:{port}/openapi.json").status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    assert time.monotonic() < deadline, "Synthetic Uvicorn readiness expired"
                    time.sleep(0.05)

            payload = {
                "projectId": "synthetic-renders-scope", "requestId": "same-render-id",
                "mode": "preview", "canonicalTimeline": {
                    "version": "1.1", "duration": {"value": 1, "timescale": 1},
                    "output": {"width": 640, "height": 360},
                    "materials": [],
                    "tracks": [{"id": "sub", "type": "subtitle", "order": 0, "clips": []}],
                },
            }
            def call(which, data):
                with httpx.Client(timeout=15, trust_env=False) as scoped_client:
                    return scoped_client.post(f"http://127.0.0.1:{ports[which]}/renders", json=data)
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = [future.result(timeout=20) for future in [
                    pool.submit(call, i % 2, payload) for i in range(8)
                ]]
            assert [res.status_code for res in responses] == [202] * 8, [
                (res.status_code, res.text[:300]) for res in responses
            ]
            assert {res.json()["jobId"] for res in responses} == {"same-render-id"}
            records = [json.loads(line) for line in dispatch_log.read_text().splitlines()]
            assert len(records) == 1, records
            assert records[0]["FAKE_ONLY"] is True
            changed = json.loads(json.dumps(payload))
            changed["canonicalTimeline"]["duration"]["value"] = 2
            changed_mode = {**payload, "mode": "final"}
            changed_project = {**payload, "projectId": "synthetic-other-project"}
            for i, altered in enumerate((changed, changed_mode, changed_project)):
                response = call(i % 2, altered)
                assert response.status_code == 409, response.text
            assert len(dispatch_log.read_text().splitlines()) == 1

            stored_path = root / "jobs" / "same-render-id.json"
            durable = json.loads(stored_path.read_text())
            assert durable["status"] == "queued"
            assert durable["requestHash"] == responses[0].json()["requestHash"]
            owner_idx = next(i for i, p in enumerate(processes) if p.pid == durable["_ownerPid"])
            processes[owner_idx].terminate()
            processes[owner_idx].wait(timeout=5)
            other_port = ports[1 - owner_idx]
            survivor = client.get(f"http://127.0.0.1:{other_port}/jobs/same-render-id")
            assert survivor.status_code == 200, survivor.text
            assert survivor.json()["status"] == "failed"
            assert survivor.json()["retryable"] is False
            assert "UNKNOWN" in survivor.json()["message"]
            identical = call(1 - owner_idx, payload)
            assert identical.status_code == 202, identical.text
            assert identical.json()["status"] == "failed"
            assert call(1 - owner_idx, changed).status_code == 409
            assert len(dispatch_log.read_text().splitlines()) == 1
            assert json.loads(stored_path.read_text())["status"] == "failed"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize(
    "other_project, other_request_hash, expected_message",
    [
        ("tenant-beta", "stable-render-input-a", "another job"),
        ("tenant-alpha", "different-render-input", "different render inputs"),
    ],
)
def test_render_id_conflict_never_mutates_foreign_or_different_input_record(
    tmp_path, other_project, other_request_hash, expected_message
):
    """FAKE-only: conflicting idempotency claims cannot mutate original job state.

    Two separate JobStore objects model a stale owner after restart; do not
    access production data or dispatch any real worker/provider operation.
    """
    from app.main import JobStore

    original = JobStore(tmp_path)
    initial, should_execute = original.create_or_get(
        "tenant-alpha", "render", "shared-render-request",
        request_hash="stable-render-input-a",
    )
    assert should_execute is True
    assert initial["status"] == "queued"
    path = tmp_path / "jobs" / "shared-render-request.json"
    before = path.read_bytes()

    other = JobStore(tmp_path)
    with pytest.raises(ValueError, match=expected_message):
        other.create_or_get(
            other_project, "render", "shared-render-request",
            request_hash=other_request_hash,
        )
    assert path.read_bytes() == before, (
        "rejected cross-tenant/request-hash replay mutated prior owner's state"
    )


def test_jobstore_unacknowledged_legacy_retry_flag_never_requeues(tmp_path):
    """FAKE-only: historical retryable=True is not an Owner-approved manual ACK.

    A previous writer may have flagged a failed paid/render task retryable.
    The normal create-or-get request must not launch another execution, even
    when its tenant and request fingerprint match.
    """
    from app.main import JobStore

    root = tmp_path / "synthetic-unacknowledged-legacy"
    original = JobStore(root)
    first, should_execute = original.create_or_get(
        "synthetic-tenant", "render", "synthetic-legacy-retry",
        request_hash="a" * 64,
    )
    assert should_execute
    original.update(
        first["jobId"],
        status="failed",
        progress=100,
        retryable=True,
        message="synthetic historical retry marker without Owner ACK",
    )
    durable_path = root / "jobs" / "synthetic-legacy-retry.json"
    before = durable_path.read_bytes()

    restarted = JobStore(root)
    replay, should_execute = restarted.create_or_get(
        "synthetic-tenant", "render", "synthetic-legacy-retry",
        request_hash="a" * 64,
    )
    assert should_execute is False, "Unacknowledged historical flag re-dispatched work"
    assert replay["status"] == "failed"
    assert durable_path.read_bytes() == before, (
        "A read/replay mutated the durable failed outcome without Owner ACK"
    )
