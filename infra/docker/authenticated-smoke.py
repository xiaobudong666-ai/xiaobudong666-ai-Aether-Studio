"""Authenticated same-origin production smoke for the complete render queue."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

BASE_URL = os.environ.get("AETHER_SMOKE_BASE_URL", "http://web/api").rstrip("/")
EMAIL = os.environ.get("AETHER_SMOKE_EMAIL", "admin@aether.local")
PASSWORD = os.environ["AETHER_SMOKE_PASSWORD"]


def response_json(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected an object from {response.request.url}")
    return payload


def main() -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and ffprobe are required for the authenticated smoke")
    with tempfile.TemporaryDirectory(prefix="aether-authenticated-smoke-") as temporary:
        source = Path(temporary) / "source.mp4"
        artifact = Path(temporary) / "render.mp4"
        run_smoke(ffmpeg, ffprobe, source, artifact)


def run_smoke(ffmpeg: str, ffprobe: str, source: Path, artifact: Path) -> None:
    subprocess.run(  # noqa: S603 - executable and argv are controlled by this CI script
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "nullsrc=s=640x360:r=24,geq=random(1)*255:128:128",
            "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ],
        check=True,
    )
    if source.stat().st_size <= 1024 * 1024:
        raise RuntimeError("Smoke media must exceed Nginx's former 1 MiB default")

    with httpx.Client(base_url=BASE_URL, timeout=120, trust_env=False) as client:
        identity = response_json(client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD}))
        csrf = {"X-Aether-CSRF": "1"}
        project = response_json(
            client.post(
                "/projects",
                headers=csrf,
                json={"name": f"CI authenticated render {uuid.uuid4().hex[:8]}"},
            )
        )
        with source.open("rb") as stream:
            uploaded = response_json(
                client.post(
                    f"/projects/{project['id']}/media",
                    headers=csrf,
                    data={"expectedRevision": str(project["revision"])},
                    files={"file": (source.name, stream, "video/mp4")},
                )
            )
        project = uploaded["project"]
        media_id = uploaded["material"]["id"]
        timeline = {
            "version": "1.1",
            "tracks": [
                {
                    "id": "ci-video", "name": "CI Video", "type": "video",
                    "clips": [
                        {
                            "id": "ci-first", "trackId": "ci-video", "materialId": media_id,
                            "start": {"value": 0, "timescale": 24},
                            "duration": {"value": 24, "timescale": 24},
                            "sourceIn": {"value": 0, "timescale": 24},
                        },
                        {
                            "id": "ci-last", "trackId": "ci-video", "materialId": media_id,
                            "start": {"value": 72, "timescale": 24},
                            "duration": {"value": 24, "timescale": 24},
                            "sourceIn": {"value": 24, "timescale": 24},
                        },
                    ],
                }
            ],
        }
        project = response_json(
            client.put(
                f"/projects/{project['id']}",
                headers=csrf,
                json={"timeline": timeline, "expectedRevision": project["revision"]},
            )
        )
        task = response_json(client.post(f"/projects/{project['id']}/render", headers=csrf))
        deadline = time.monotonic() + 180
        completed = None
        while time.monotonic() < deadline:
            tasks_response = client.get("/render-tasks", params={"projectId": project["id"]})
            tasks_response.raise_for_status()
            tasks = tasks_response.json()
            current = next(item for item in tasks if item["taskId"] == task["taskId"])
            if current["status"] == "failed":
                raise RuntimeError(f"Persistent render task failed: {current}")
            if current["status"] == "completed":
                completed = current
                break
            time.sleep(1)
        if completed is None:
            raise RuntimeError("Persistent render task did not complete before the smoke timeout")

        artifact_path = str(completed["artifactUrl"])
        if BASE_URL.endswith("/api") and artifact_path.startswith("/api/"):
            artifact_path = artifact_path.removeprefix("/api")
        download = client.get(artifact_path)
        download.raise_for_status()
        artifact.write_bytes(download.content)
        metadata = json.loads(
            subprocess.run(  # noqa: S603 - executable and argv are controlled by this CI script
                [
                    ffprobe, "-v", "error", "-show_entries", "format=duration",
                    "-of", "json", str(artifact),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        duration = float(metadata["format"]["duration"])
        if abs(duration - 4.0) > 0.2:
            raise RuntimeError(f"Canonical gap render duration is {duration}, expected 4 seconds")
        me = response_json(client.get("/auth/me"))
        if me["tenant"]["id"] != identity["tenant"]["id"]:
            raise RuntimeError("Authenticated tenant changed during the smoke flow")

    print(
        json.dumps(
            {
                "status": "passed",
                "sourceBytes": source.stat().st_size,
                "renderBytes": artifact.stat().st_size,
                "durationSeconds": duration,
                "taskId": task["taskId"],
            },
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
