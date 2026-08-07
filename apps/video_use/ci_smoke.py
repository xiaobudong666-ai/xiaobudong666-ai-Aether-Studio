"""Bounded real-render smoke test used inside the Docker CI service."""

from __future__ import annotations

import time

import httpx


def main() -> None:
    payload = {
        "projectId": "ci",
        "ranges": [{"mediaId": "ci-source", "start": 0, "end": 0.8}],
        "mode": "draft",
        "grade": "subtle",
        "normalizeAudio": False,
    }
    with httpx.Client(base_url="http://127.0.0.1:8002", timeout=60, trust_env=False) as client:
        response = client.post("/renders", json=payload)
        response.raise_for_status()
        job_id = response.json()["jobId"]
        result = None
        for _ in range(120):
            result = client.get(f"/jobs/{job_id}").json()
            if result["status"] in {"completed", "failed"}:
                break
            time.sleep(1)
        if not result or result["status"] != "completed":
            raise RuntimeError(f"video-use smoke render failed: {result}")
        artifact = client.get(f"/jobs/{job_id}/artifact")
        artifact.raise_for_status()
        if len(artifact.content) <= 1_000:
            raise RuntimeError("video-use smoke render returned an empty artifact")
        print(
            {
                "jobId": job_id,
                "artifactBytes": len(artifact.content),
                "metadata": result.get("metadata"),
            }
        )


if __name__ == "__main__":
    main()
