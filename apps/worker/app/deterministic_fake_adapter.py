"""Strictly-local deterministic-fake Worker adapter.

Default-off. Zero Provider network, zero credentials, zero billing, zero
external process except a local FFmpeg invocation that synthesizes a
deterministic test-pattern MP4. The adapter exposes the same narrow surface as
``MoneyPrinterTurboAdapter`` (``generate_video`` / ``get_task_status`` /
``stream_artifact`` plus optional health/capabilities) so that a fake-only
Generation flows through the exact same Worker
claim/transition/artifact-intake topology without touching a real Provider.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import threading
from typing import BinaryIO

ADAPTER_VERSION = "aether-deterministic-fake-v1"

_ASPECT_DIMENSIONS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}

# Embedded tone used for the fake talking-head source. It is intentionally
# different from the narration tone used in golden-chain tests so audio
# authority (independent narration overrides embedded source) stays provable.
_SOURCE_TONE_HZ = 1000


class DeterministicFakeError(RuntimeError):
    """Sanitized boundary for a local fake generation failure."""


class DeterministicFakeAdapter:
    """Deterministic, offline, in-process fake generation adapter."""

    def __init__(
        self,
        ffmpeg_binary: str = "ffmpeg",
        timeout_seconds: int = 180,
    ):
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        self.ffmpeg_binary = ffmpeg_binary
        self.timeout_seconds = timeout_seconds
        self._jobs: dict[str, dict] = {}
        self._artifacts: dict[str, bytes] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ health
    def check_health(self) -> dict:
        available = shutil.which(self.ffmpeg_binary) is not None
        return {
            "status": "healthy" if available else "unhealthy",
            "service": "deterministic-fake",
            "responsive": available,
            "adapterVersion": ADAPTER_VERSION,
            "network": "none",
        }

    def get_capabilities(self) -> dict:
        health = self.check_health()
        healthy = health["status"] == "healthy"
        return {
            "status": "active" if healthy else "degraded",
            "healthy": healthy,
            "adapterVersion": ADAPTER_VERSION,
            "capabilities": {
                "videoAspects": ["9:16", "16:9", "1:1"],
                "videoConcatModes": ["random", "sequential"],
                "maxOutputs": 1,
                "maxClipDurationSeconds": 10,
                "cancellationSupported": False,
                "artifactStreaming": True,
                "network": False,
            },
        }

    # --------------------------------------------------------------- lifecycle
    def generate_video(
        self,
        subject: str,
        aspect: str = "9:16",
        voice_name: str = "en-US-JennyNeural",
        video_concat_mode: str = "random",
        video_clip_duration: int = 5,
    ) -> str:
        if not subject.strip() or len(subject) > 500:
            raise DeterministicFakeError("Generation subject rejected")
        if aspect not in _ASPECT_DIMENSIONS:
            raise DeterministicFakeError("Generation aspect rejected")
        if video_concat_mode not in {"random", "sequential"} or not 1 <= int(video_clip_duration) <= 10:
            raise DeterministicFakeError("Generation options rejected")

        seed = "|".join(
            (
                subject,
                aspect,
                voice_name,
                video_concat_mode,
                str(int(video_clip_duration)),
            )
        )
        job_id = "fake-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]
        with self._lock:
            self._jobs[job_id] = {
                "aspect": aspect,
                "duration": max(1, min(10, int(video_clip_duration))),
            }
        return job_id

    def get_task_status(self, task_id: str) -> dict:
        if not task_id or any(marker in task_id for marker in ("/", "\\", "..", ":")):
            raise DeterministicFakeError("Provider task identifier rejected")
        with self._lock:
            known = task_id in self._jobs
        if not known:
            raise DeterministicFakeError("Provider task identifier is unknown")
        # The fake generator is synchronous and deterministic: a claimed task is
        # complete on its first status poll, which yields 1 attempt / 0 retry /
        # 0 fallback in the Worker topology.
        return {
            "task_id": task_id,
            "status": "completed",
            "progress": 100,
            "providerArtifactId": task_id,
        }

    def cancel_task(self, _task_id: str) -> None:
        # Local fake generation never needs an async cancel path.
        return None

    def stream_artifact(self, provider_artifact_id: str) -> BinaryIO:
        with self._lock:
            job = self._jobs.get(provider_artifact_id)
        if job is None:
            raise DeterministicFakeError("Artifact identifier is unknown")
        payload = self._render(job)
        return io.BytesIO(payload)

    # ----------------------------------------------------------------- render
    def _render(self, job: dict) -> bytes:
        key = f"{job['aspect']}:{job['duration']}"
        with self._lock:
            cached = self._artifacts.get(key)
        if cached is not None:
            return cached

        if shutil.which(self.ffmpeg_binary) is None:
            raise DeterministicFakeError(
                "Local FFmpeg is unavailable; deterministic-fake requires an offline encoder"
            )
        width, height = _ASPECT_DIMENSIONS[job["aspect"]]
        duration = job["duration"]

        # MP4 needs a seekable output; a pipe cannot carry the moov atom.
        # Encode into a private temporary file (strictly local, zero network)
        # and read the bytes back for streaming through artifact-intake.
        fd, out_path = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        command = [
            self.ffmpeg_binary, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=24",
            "-f", "lavfi", "-i", f"sine=frequency={_SOURCE_TONE_HZ}:sample_rate=48000",
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-shortest",
            "-movflags", "+faststart", out_path,
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
            with open(out_path, "rb") as handle:
                payload = handle.read()
        except FileNotFoundError as exc:
            raise DeterministicFakeError("Local FFmpeg is unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise DeterministicFakeError("Local fake encode exceeded the time limit") from exc
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or exc.stdout or b"FFmpeg failed").decode(
                "utf-8", "replace"
            )
            raise DeterministicFakeError(diagnostic[-2000:]) from exc
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

        if not payload:
            raise DeterministicFakeError("Local fake encode produced an empty artifact")
        with self._lock:
            self._artifacts[key] = payload
        return payload
