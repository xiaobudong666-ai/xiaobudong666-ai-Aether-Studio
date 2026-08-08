import subprocess
import time
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
