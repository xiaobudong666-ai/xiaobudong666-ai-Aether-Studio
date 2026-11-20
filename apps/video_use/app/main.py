from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger("video_use_service")

UPSTREAM_REPOSITORY = "https://github.com/browser-use/video-use"
UPSTREAM_COMMIT = "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66"
UPSTREAM_VERSION = "0.1.0"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
ALLOWED_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".m4v",
    ".avi",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_FORMATS = {"image2", "image2pipe", "png_pipe", "jpeg_pipe", "webp_pipe"}
MAX_UPLOAD_BYTES = int(os.environ.get("VIDEO_USE_MAX_UPLOAD_BYTES", str(2 * 1024**3)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def idempotent_media_id(project_id: str, idempotency_key: str) -> str:
    """Stable Sidecar media identity for a single governed generation task."""
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"aether-video-use:{project_id}:{idempotency_key}",
    ))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    """Persist a preceding POSIX directory-entry mutation before returning."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class RenderRange(BaseModel):
    mediaId: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("mediaId")
    @classmethod
    def media_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, "mediaId")

    @field_validator("end")
    @classmethod
    def range_is_nonempty(cls, value: float, info):
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("结束时间必须晚于开始时间")
        return value


class RationalValue(BaseModel):
    value: int
    timescale: int = Field(gt=0)

    def fraction(self) -> Fraction:
        return Fraction(self.value, self.timescale)


class CanonicalMaterial(BaseModel):
    id: str
    type: Literal["video", "audio", "image"]


class CanonicalClip(BaseModel):
    id: str
    materialId: str
    start: RationalValue
    duration: RationalValue
    sourceIn: RationalValue
    volume: float = Field(default=1, ge=0, le=4)
    opacity: float = Field(default=1, ge=0, le=1)
    x: int = 0
    y: int = 0
    width: int | None = Field(default=None, gt=0, le=7680)
    height: int | None = Field(default=None, gt=0, le=4320)
    text: str | None = Field(default=None, max_length=2_000)


class CanonicalTrack(BaseModel):
    id: str
    type: Literal["video", "audio", "subtitle"]
    order: int = Field(ge=0)
    clips: list[CanonicalClip] = Field(max_length=500)


class CanonicalOutput(BaseModel):
    width: int = Field(default=1920, ge=16, le=7680)
    height: int = Field(default=1080, ge=16, le=4320)
    fps: RationalValue = RationalValue(value=24, timescale=1)
    backgroundColor: str = Field(default="black", pattern=r"^[A-Za-z]+$|^#[0-9A-Fa-f]{6}$")


class CanonicalTimeline(BaseModel):
    version: Literal["1.1"] = "1.1"
    duration: RationalValue
    output: CanonicalOutput = CanonicalOutput()
    materials: list[CanonicalMaterial] = Field(max_length=500)
    tracks: list[CanonicalTrack] = Field(max_length=100)


class RenderJobRequest(BaseModel):
    projectId: str
    requestId: str | None = None
    ranges: list[RenderRange] = Field(default_factory=list, max_length=500)
    canonicalTimeline: CanonicalTimeline | None = None
    mode: Literal["draft", "preview", "final"] = "preview"
    grade: Literal["auto", "subtle", "neutral_punch", "warm_cinematic"] = "auto"
    normalizeAudio: bool = True

    @field_validator("projectId", "requestId")
    @classmethod
    def identifiers_are_safe(cls, value: str | None, info) -> str | None:
        return validate_identifier(value, info.field_name) if value is not None else value

    @model_validator(mode="after")
    def has_render_definition(self):
        if not self.ranges and self.canonicalTimeline is None:
            raise ValueError("必须提供剪辑范围或标准时间线")
        return self


class TranscriptionJobRequest(BaseModel):
    projectId: str
    mediaId: str
    language: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]+)?$")
    numSpeakers: int | None = Field(default=None, ge=1, le=32)

    @field_validator("projectId", "mediaId")
    @classmethod
    def identifiers_are_safe(cls, value: str, info) -> str:
        return validate_identifier(value, info.field_name)


class TimelineViewJobRequest(BaseModel):
    projectId: str
    mediaId: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    frames: int = Field(default=10, ge=1, le=24)

    @field_validator("projectId", "mediaId")
    @classmethod
    def identifiers_are_safe(cls, value: str, info) -> str:
        return validate_identifier(value, info.field_name)

    @field_validator("end")
    @classmethod
    def range_is_nonempty(cls, value: float, info):
        start = info.data.get("start")
        if start is not None and value <= start:
            raise ValueError("结束时间必须晚于开始时间")
        return value


class JobPersistenceUnknownError(ValueError):
    """A durable job outcome cannot be safely inferred without reconciliation."""


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.instances_dir = self.jobs_dir / "instances"
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        self._instance_id = uuid.uuid4().hex
        self._instance_lock_path = self.instances_dir / f"{self._instance_id}.lock"
        self._instance_lock = self._instance_lock_path.open("a+", encoding="utf-8")
        fcntl.flock(self._instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._process_lock_path = self.jobs_dir / "jobstore.lock"
        with self._lock, self._cross_process_lock():
            for path in self.jobs_dir.glob("*.json"):
                try:
                    payload = self._read_path(path)
                    job_id = payload["jobId"]
                    if self._pending_path(job_id).exists():
                        logger.error(
                            "Persisted job %s has an UNKNOWN pending outcome; retaining fence",
                            job_id,
                        )
                        self._jobs[job_id] = payload
                        continue
                    legacy_unsafe_retry = (
                        payload.get("status") == "failed"
                        and payload.get("retryable") is True
                        and payload.get("message") == "渲染服务重启导致任务中断，可以安全重试"
                    )
                    if legacy_unsafe_retry:
                        payload.update(
                            status="failed",
                            progress=100,
                            message=(
                                "Sidecar任务重启后执行结果 UNKNOWN；"
                                "须人工核验后再决定是否重试"
                            ),
                            retryable=False,
                            updatedAt=utc_now(),
                        )
                        self._persist_unlocked(payload)
                    self._jobs[job_id] = payload
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    logger.exception("Invalid persisted job file retained for review: %s", path)
            for path in self.jobs_dir.glob("*.json.pending"):
                logger.error("UNKNOWN persisted job outcome retained at %s", path)

    @contextmanager
    def _cross_process_lock(self):
        with self._process_lock_path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _target_path(self, job_id: str) -> Path:
        validate_identifier(job_id, "jobId")
        return self.jobs_dir / f"{job_id}.json"

    def _pending_path(self, job_id: str) -> Path:
        return self._target_path(job_id).with_suffix(".json.pending")

    def _read_path(self, path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("jobId") != path.name.removesuffix(".json")
        ):
            raise ValueError("persisted job identity mismatch")
        return payload

    def _read_final_unlocked(self, job_id: str) -> dict[str, Any] | None:
        path = self._target_path(job_id)
        if not path.is_file():
            return None
        try:
            return self._read_path(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise JobPersistenceUnknownError(
                "requestId has an UNKNOWN persisted outcome; manual reconciliation required"
            ) from exc

    def _assert_no_pending_unlocked(self, job_id: str) -> None:
        if self._pending_path(job_id).exists():
            raise JobPersistenceUnknownError(
                "requestId has an UNKNOWN persisted outcome; manual reconciliation required"
            )

    def _owner_is_alive(self, owner_id: Any, owner_pid: Any) -> bool:
        if (
            not isinstance(owner_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", owner_id) is None
        ):
            return False
        if owner_id == self._instance_id:
            return True
        # A second store in one interpreter is the native regression harness's
        # restart model, not an independently executing OS-process peer.
        if owner_pid == os.getpid():
            return False
        path = self.instances_dir / f"{owner_id}.lock"
        try:
            stream = path.open("a+", encoding="utf-8")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            stream.close()

    def _recover_abandoned_unlocked(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (
            payload.get("status") in {"queued", "processing"}
            and not self._owner_is_alive(
                payload.get("_ownerInstanceId"),
                payload.get("_ownerPid"),
            )
        ):
            payload = dict(payload)
            payload.update(
                status="failed",
                progress=100,
                message=(
                    "任务执行进程已退出，结果为 UNKNOWN；已隔离，"
                    "需人工核对后再决定是否重试"
                ),
                retryable=False,
                updatedAt=utc_now(),
            )
            self._persist_unlocked(payload)
        return payload

    @staticmethod
    def _public_payload(payload: dict[str, Any]) -> dict[str, Any]:
        public = dict(payload)
        public.pop("_ownerInstanceId", None)
        public.pop("_ownerPid", None)
        return public

    def create(self, project_id: str, kind: str, job_id: str | None = None) -> dict[str, Any]:
        payload, _should_execute = self.create_or_get(project_id, kind, job_id)
        return payload

    def create_or_get(
        self,
        project_id: str,
        kind: str,
        job_id: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_id = job_id or str(uuid.uuid4())
        validate_identifier(job_id, "jobId")
        with self._lock, self._cross_process_lock():
            self._assert_no_pending_unlocked(job_id)
            existing = self._read_final_unlocked(job_id)
            if existing is not None:
                # Validate the immutable project/kind/input binding BEFORE any
                # abandoned-owner recovery, which can mutate durable state.
                # A conflicting tenant or render body is never authorized to
                # trigger another Owner's UNKNOWN status transition.
                if existing.get("projectId") != project_id or existing.get("kind") != kind:
                    raise ValueError("requestId is already bound to another job")
                # Legacy records without a digest must not authorize output reuse.
                if request_hash is not None and existing.get("requestHash") != request_hash:
                    raise ValueError("requestId is already bound to different render inputs")
                existing = self._recover_abandoned_unlocked(existing)
                self._jobs[job_id] = existing
                # A historical retryable flag is not a durable, verified Owner
                # acknowledgement. Replaying the normal requestId must NEVER
                # create a new execution after an uncertain failed outcome.
                # Explicit manual reconciliation remains a separate gated path.
                return self._public_payload(existing), False
            payload = {
                "jobId": job_id,
                "projectId": project_id,
                "kind": kind,
                "status": "queued",
                "progress": 0,
                "message": "任务已排队",
                "createdAt": utc_now(),
                "updatedAt": utc_now(),
                "artifact": None,
                "retryable": False,
                "_ownerInstanceId": self._instance_id,
                "_ownerPid": os.getpid(),
            }
            if request_hash is not None:
                payload["requestHash"] = request_hash
            self._jobs[job_id] = payload
            self._persist_unlocked(payload)
        return self._public_payload(payload), True

    def update(self, job_id: str, **values: Any) -> dict[str, Any]:
        validate_identifier(job_id, "jobId")
        with self._lock, self._cross_process_lock():
            self._assert_no_pending_unlocked(job_id)
            current = self._read_final_unlocked(job_id)
            if current is None:
                raise KeyError(job_id)
            if current.get("_ownerInstanceId") != self._instance_id:
                raise JobPersistenceUnknownError(
                    "non-owner job writer rejected; persisted outcome requires reconciliation"
                )
            expected = self._jobs.get(job_id)
            if expected is not None and current != expected:
                raise JobPersistenceUnknownError(
                    "stale job writer rejected; persisted outcome requires reconciliation"
                )
            if current.get("status") in {"completed", "failed"}:
                raise JobPersistenceUnknownError(
                    "terminal job writer rejected; persisted outcome requires reconciliation"
                )
            payload = dict(current)
            payload.update(values, updatedAt=utc_now())
            self._persist_unlocked(payload)
            self._jobs[job_id] = payload
            return self._public_payload(payload)

    def get(self, job_id: str) -> dict[str, Any] | None:
        validate_identifier(job_id, "jobId")
        with self._lock, self._cross_process_lock():
            self._assert_no_pending_unlocked(job_id)
            payload = self._read_final_unlocked(job_id)
            if payload is None:
                return None
            payload = self._recover_abandoned_unlocked(payload)
            self._jobs[job_id] = payload
        return self._public_payload(payload)

    def _persist(self, payload: dict[str, Any]) -> None:
        with self._lock, self._cross_process_lock():
            self._assert_no_pending_unlocked(payload["jobId"])
            self._persist_unlocked(payload)

    def _persist_unlocked(self, payload: dict[str, Any]) -> None:
        target = self._target_path(payload["jobId"])
        temporary = self._pending_path(payload["jobId"])
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise JobPersistenceUnknownError(
                "requestId has an UNKNOWN persisted outcome; manual reconciliation required"
            ) from exc
        try:
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError(errno.EIO, "job persistence write made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
        except OSError as exc:
            # The atomic reservation already exists. Any write/fsync/replace
            # failure therefore has an ambiguous durable outcome and must
            # retain that native fence instead of leaking a generic 500.
            raise JobPersistenceUnknownError(
                "requestId has an UNKNOWN persisted outcome; manual reconciliation required"
            ) from exc
        try:
            fsync_directory(self.jobs_dir)
        except OSError as exc:
            # Promotion visibility/durability is ambiguous. Recreate the same
            # native pending fence so this live process and its peers reject
            # replay until an operator reconciles the final bytes.
            marker = json.dumps(
                {**payload, "_persistenceUnknown": True},
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            try:
                marker_descriptor = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                try:
                    remaining = memoryview(marker)
                    while remaining:
                        written = os.write(marker_descriptor, remaining)
                        if written <= 0:
                            raise OSError(
                                errno.EIO,
                                "job UNKNOWN marker write made no progress",
                            )
                        remaining = remaining[written:]
                    os.fsync(marker_descriptor)
                finally:
                    os.close(marker_descriptor)
                try:
                    fsync_directory(self.jobs_dir)
                except OSError:
                    logger.exception("Unable to fsync job UNKNOWN marker directory")
            raise JobPersistenceUnknownError(
                "requestId has an UNKNOWN persisted outcome; manual reconciliation required"
            ) from exc


class MediaStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        validate_identifier(project_id, "projectId")
        path = (self.root / "projects" / project_id).resolve()
        path.relative_to(self.root)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def media_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "media"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def find_media(self, project_id: str, media_id: str) -> Path:
        validate_identifier(media_id, "mediaId")
        matches = list(self.media_dir(project_id).glob(f"{media_id}.*"))
        if len(matches) != 1 or not matches[0].is_file():
            raise FileNotFoundError(media_id)
        resolved = matches[0].resolve()
        resolved.relative_to(self.root)
        return resolved

    def job_dir(self, project_id: str, job_id: str) -> Path:
        validate_identifier(job_id, "jobId")
        path = (self.project_dir(project_id) / "renders" / job_id).resolve()
        path.relative_to(self.root)
        path.mkdir(parents=True, exist_ok=True)
        return path


def run_checked(command: list[str], timeout: int = 3_600) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - argv is constructed internally; shell=False
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required executable is unavailable: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Operation exceeded {timeout} seconds") from exc
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or "operation failed").strip()
        raise RuntimeError(diagnostic[-4_000:]) from exc


def probe(path: Path) -> dict[str, Any]:
    result = run_checked(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    payload = json.loads(result.stdout)
    duration = payload.get("format", {}).get("duration")
    video = next(
        (item for item in payload.get("streams", []) if item.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (item for item in payload.get("streams", []) if item.get("codec_type") == "audio"),
        None,
    )
    return {
        "durationSeconds": float(duration) if duration is not None else None,
        "sizeBytes": int(payload.get("format", {}).get("size", 0) or 0),
        "format": payload.get("format", {}).get("format_name"),
        "video": (
            {
                "codec": video.get("codec_name"),
                "width": video.get("width"),
                "height": video.get("height"),
            }
            if video
            else None
        ),
        "audio": (
            {
                "codec": audio.get("codec_name"),
                "sampleRate": int(audio.get("sample_rate", 0) or 0),
                "channels": audio.get("channels"),
            }
            if audio
            else None
        ),
    }


def create_app(
    media_root: Path | None = None,
    upstream_root: Path | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> FastAPI:
    root = Path(
        media_root
        or os.environ.get("VIDEO_USE_MEDIA_ROOT", ".local/video-use-media")
    )
    upstream = Path(upstream_root or os.environ.get("VIDEO_USE_UPSTREAM_ROOT", "/opt/video-use"))
    store = MediaStore(root)
    jobs = JobStore(root)
    upload_idempotency_lock = asyncio.Lock()
    pool = executor or ThreadPoolExecutor(
        max_workers=max(1, int(os.environ.get("VIDEO_USE_WORKERS", "2"))),
        thread_name_prefix="video-use",
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if executor is None:
            pool.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title="Aether video-use Sidecar", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(JobPersistenceUnknownError)
    async def persistence_unknown_handler(_request, exc: JobPersistenceUnknownError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def helper(name: str) -> Path:
        path = (upstream / "helpers" / name).resolve()
        try:
            path.relative_to(upstream.resolve())
        except ValueError as exc:  # pragma: no cover - construction guard
            raise RuntimeError("Invalid upstream helper path") from exc
        if not path.is_file():
            raise RuntimeError(f"video-use helper is missing: {name}")
        return path

    def fail_job(job_id: str, exc: Exception) -> None:
        logger.exception("video-use job %s failed", job_id)
        message = str(exc).strip() or type(exc).__name__
        jobs.update(job_id, status="failed", progress=100, message=message[-2_000:], retryable=False)

    def ffmpeg_number(value: Fraction) -> str:
        return f"{float(value):.9f}".rstrip("0").rstrip(".") or "0"

    def execute_canonical_render(
        job_id: str,
        job_dir: Path,
        request: RenderJobRequest,
        output: Path,
    ) -> None:
        timeline = request.canonicalTimeline
        if timeline is None:  # pragma: no cover - guarded by caller
            raise RuntimeError("Canonical Timeline is missing")
        duration = timeline.duration.fraction()
        if duration <= 0:
            raise RuntimeError("Canonical Timeline duration must be positive")
        width = timeline.output.width - (timeline.output.width % 2)
        height = timeline.output.height - (timeline.output.height % 2)
        fps = timeline.output.fps.fraction()
        if fps <= 0:
            raise RuntimeError("Output FPS must be positive")
        duration_text = ffmpeg_number(duration)
        fps_text = ffmpeg_number(fps)
        material_types = {item.id: item.type for item in timeline.materials}

        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"color=c={timeline.output.backgroundColor}:s={width}x{height}:r={fps_text}:d={duration_text}",
            "-f", "lavfi", "-i",
            f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration_text}",
        ]
        prepared: list[tuple[CanonicalTrack, CanonicalClip, int, dict[str, Any]]] = []
        for track in sorted(timeline.tracks, key=lambda item: item.order):
            for clip in sorted(track.clips, key=lambda item: item.start.fraction()):
                if clip.duration.fraction() <= 0:
                    continue
                if track.type == "subtitle":
                    prepared.append((track, clip, -1, {}))
                    continue
                if clip.materialId not in material_types:
                    raise RuntimeError(f"Unknown Canonical Timeline material: {clip.materialId}")
                source = store.find_media(request.projectId, clip.materialId)
                metadata = probe(source)
                input_index = 2 + sum(1 for _track, _clip, index, _meta in prepared if index >= 0)
                command.extend(["-i", str(source)])
                prepared.append((track, clip, input_index, metadata))

        filters = ["[0:v]format=yuv420p[canvas0]", "[1:a]anull[silence0]"]
        canvas_label = "canvas0"
        audio_labels = ["silence0"]
        video_index = 0
        audio_index = 0
        subtitle_index = 0
        for track, clip, input_index, metadata in prepared:
            start = clip.start.fraction()
            clip_duration = clip.duration.fraction()
            source_in = clip.sourceIn.fraction()
            end = start + clip_duration
            start_text = ffmpeg_number(start)
            duration_clip_text = ffmpeg_number(clip_duration)
            source_text = ffmpeg_number(source_in)
            end_text = ffmpeg_number(end)

            if track.type == "subtitle":
                if not clip.text:
                    continue
                text_path = job_dir / f"subtitle_{subtitle_index:03d}.txt"
                text_path.write_text(clip.text, encoding="utf-8")
                next_label = f"subtitle{subtitle_index + 1}"
                filters.append(
                    f"[{canvas_label}]drawtext=textfile='{text_path}':fontcolor=white:fontsize=48:"
                    "box=1:boxcolor=black@0.55:boxborderw=16:x=(w-text_w)/2:y=h-text_h-80:"
                    f"enable='between(t,{start_text},{end_text})'[{next_label}]"
                )
                canvas_label = next_label
                subtitle_index += 1
                continue

            if track.type == "video" and metadata.get("video"):
                target_width = clip.width or width
                target_height = clip.height or height
                clip_label = f"clipv{video_index}"
                next_canvas = f"canvas{video_index + 1}"
                filters.append(
                    f"[{input_index}:v]trim=start={source_text}:duration={duration_clip_text},"
                    f"setpts=PTS-STARTPTS+{start_text}/TB,"
                    f"scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                    f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
                    f"format=yuva420p,colorchannelmixer=aa={clip.opacity:.6f}[{clip_label}]"
                )
                filters.append(
                    f"[{canvas_label}][{clip_label}]overlay=x={clip.x}:y={clip.y}:eof_action=pass:shortest=0:"
                    f"enable='between(t,{start_text},{end_text})'[{next_canvas}]"
                )
                canvas_label = next_canvas
                video_index += 1

            if metadata.get("audio") and track.type in {"video", "audio"}:
                label = f"clipa{audio_index}"
                filters.append(
                    f"[{input_index}:a]atrim=start={source_text}:duration={duration_clip_text},"
                    f"asetpts=PTS-STARTPTS+{start_text}/TB,volume={clip.volume:.6f}[{label}]"
                )
                audio_labels.append(label)
                audio_index += 1

        filters.append(
            "".join(f"[{label}]" for label in audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0,"
            + f"atrim=duration={duration_text}[audioout]"
        )
        command.extend(
            [
                "-filter_complex", ";".join(filters),
                "-map", f"[{canvas_label}]", "-map", "[audioout]",
                "-t", duration_text, "-r", fps_text,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(output),
            ]
        )
        (job_dir / "canonical-timeline.json").write_text(
            timeline.model_dump_json(indent=2), encoding="utf-8"
        )
        (job_dir / "ffmpeg-command.json").write_text(
            json.dumps(command, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        jobs.update(job_id, progress=25, message="正在使用 FFmpeg 渲染标准时间线")
        run_checked(command)

    def execute_render(job: dict[str, Any], request: RenderJobRequest) -> None:
        job_id = job["jobId"]
        try:
            jobs.update(job_id, status="processing", progress=10, message="正在准备剪辑清单")
            job_dir = store.job_dir(request.projectId, job_id)
            output = job_dir / "final.mp4"
            if request.canonicalTimeline is not None:
                execute_canonical_render(job_id, job_dir, request, output)
            else:
                source_aliases: dict[str, str] = {}
                media_aliases: dict[str, str] = {}
                ranges: list[dict[str, Any]] = []
                for item in request.ranges:
                    alias = media_aliases.get(item.mediaId)
                    if alias is None:
                        source = store.find_media(request.projectId, item.mediaId)
                        alias = f"source_{len(media_aliases):03d}"
                        media_aliases[item.mediaId] = alias
                        source_aliases[alias] = str(source)
                    ranges.append({"source": alias, "start": item.start, "end": item.end, "note": item.note or ""})
                edl = {"sources": source_aliases, "ranges": ranges, "grade": request.grade}
                edl_path = job_dir / "edl.json"
                edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2))
                command = [sys.executable, str(helper("render.py")), str(edl_path), "-o", str(output)]
                if request.mode == "draft":
                    command.append("--draft")
                elif request.mode == "preview":
                    command.append("--preview")
                command.append("--no-subtitles")
                if not request.normalizeAudio:
                    command.append("--no-loudnorm")
                jobs.update(job_id, progress=25, message="正在使用固定版本的 video-use 渲染")
                run_checked(command)
            metadata = probe(output)
            # Freeze a digest and byte count into the durable terminal record.
            # These are Sidecar-local integrity observations, not a signed
            # independent acceptance receipt or a billing settlement.
            artifact_size = output.stat().st_size
            artifact_sha256 = sha256_file(output)
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="成片渲染完成",
                artifact=str(output.relative_to(store.root)),
                artifactSizeBytes=artifact_size,
                artifactSha256=artifact_sha256,
                metadata=metadata,
            )
        except Exception as exc:  # pragma: no cover - assertion via public API
            fail_job(job_id, exc)

    def execute_transcription(job: dict[str, Any], request: TranscriptionJobRequest) -> None:
        job_id = job["jobId"]
        try:
            if not os.environ.get("ELEVENLABS_API_KEY"):
                raise RuntimeError("ELEVENLABS_API_KEY is not configured")
            source = store.find_media(request.projectId, request.mediaId)
            project_dir = store.project_dir(request.projectId)
            command = [
                sys.executable,
                str(helper("transcribe.py")),
                str(source),
                "--edit-dir",
                str(project_dir / "edit"),
            ]
            if request.language:
                command.extend(["--language", request.language])
            if request.numSpeakers:
                command.extend(["--num-speakers", str(request.numSpeakers)])
            jobs.update(job_id, status="processing", progress=20, message="正在生成字幕文本")
            run_checked(command, timeout=1_800)
            transcript = project_dir / "edit" / "transcripts" / f"{source.stem}.json"
            if not transcript.is_file():
                raise RuntimeError("Transcription completed without an artifact")
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="字幕文本生成完成",
                artifact=str(transcript.relative_to(store.root)),
            )
        except Exception as exc:  # pragma: no cover - assertion via public API
            fail_job(job_id, exc)

    def execute_timeline_view(job: dict[str, Any], request: TimelineViewJobRequest) -> None:
        job_id = job["jobId"]
        try:
            source = store.find_media(request.projectId, request.mediaId)
            job_dir = store.job_dir(request.projectId, job_id)
            output = job_dir / "timeline.png"
            jobs.update(job_id, status="processing", progress=20, message="正在生成时间线视图")
            run_checked(
                [
                    sys.executable,
                    str(helper("timeline_view.py")),
                    str(source),
                    str(request.start),
                    str(request.end),
                    "--n-frames",
                    str(request.frames),
                    "-o",
                    str(output),
                ],
                timeout=600,
            )
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="时间线视图生成完成",
                artifact=str(output.relative_to(store.root)),
            )
        except Exception as exc:  # pragma: no cover - assertion via public API
            fail_job(job_id, exc)

    @app.get("/health")
    def health():
        render_helper = upstream / "helpers" / "render.py"
        healthy = shutil.which("ffmpeg") and shutil.which("ffprobe") and render_helper.is_file()
        return {
            "status": "healthy" if healthy else "unhealthy",
            "service": "video-use",
            "upstreamCommit": UPSTREAM_COMMIT,
            "renderHelper": render_helper.is_file(),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "ffprobe": shutil.which("ffprobe") is not None,
        }

    @app.get("/capabilities")
    def capabilities():
        return {
            "repository": UPSTREAM_REPOSITORY,
            "version": UPSTREAM_VERSION,
            "commit": UPSTREAM_COMMIT,
            "license": "MIT",
            "render": True,
            "timelineView": (upstream / "helpers" / "timeline_view.py").is_file(),
            "transcription": {
                "available": (upstream / "helpers" / "transcribe.py").is_file(),
                "configured": bool(os.environ.get("ELEVENLABS_API_KEY")),
                "provider": "ElevenLabs Scribe",
            },
            "modes": ["draft", "preview", "final"],
            "grades": ["auto", "subtle", "neutral_punch", "warm_cinematic"],
        }

    @app.post("/media", status_code=status.HTTP_201_CREATED)
    async def upload_media(
        projectId: str = Form(...),
        file: UploadFile = File(...),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        content_sha256: str | None = Header(default=None, alias="X-Content-SHA256"),
    ):
        try:
            validate_identifier(projectId, "projectId")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        filename = Path(file.filename or "").name
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=415, detail="不支持该媒体文件扩展名")
        if (idempotency_key is None) != (content_sha256 is None):
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key and X-Content-SHA256 must be supplied together",
            )
        if idempotency_key is not None:
            if (
                not 1 <= len(idempotency_key) <= 256
                or any(ord(char) < 33 or ord(char) > 126 for char in idempotency_key)
            ):
                raise HTTPException(status_code=422, detail="Idempotency-Key format is invalid")
            if (
                len(content_sha256 or "") != 64
                or any(char not in "0123456789abcdefABCDEF" for char in (content_sha256 or ""))
            ):
                raise HTTPException(status_code=422, detail="X-Content-SHA256 format is invalid")
            media_id = idempotent_media_id(projectId, idempotency_key)
        else:
            media_id = str(uuid.uuid4())

        media_dir = store.media_dir(projectId)
        destination = media_dir / f"{media_id}{suffix}"
        # ffprobe uses the final extension; keep it at the end of the temporary name.
        temporary = media_dir / f".{media_id}.{uuid.uuid4().hex}.pending{suffix}"
        idempotent_replay = False
        size = 0
        digest = hashlib.sha256()
        metadata: dict[str, Any] = {}
        media_type = "video"

        async def persist_upload() -> dict[str, Any]:
            nonlocal idempotent_replay, size, metadata, media_type, destination
            try:
                with temporary.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="媒体文件过大")
                        digest.update(chunk)
                        output.write(chunk)

                actual_sha256 = digest.hexdigest()
                if content_sha256 is not None and actual_sha256 != content_sha256.lower():
                    raise HTTPException(
                        status_code=422,
                        detail="Uploaded bytes do not match X-Content-SHA256",
                    )

                metadata = probe(temporary)
                if suffix in IMAGE_EXTENSIONS:
                    video = metadata.get("video") or {}
                    formats = set(str(metadata.get("format") or "").split(","))
                    if (
                        not video.get("width")
                        or not video.get("height")
                        or metadata.get("audio") is not None
                        or not formats.intersection(IMAGE_FORMATS)
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail="图片文件未通过图像格式与尺寸校验",
                        )
                    media_type = "image"
                else:
                    media_type = "video" if metadata.get("video") else "audio"

                if idempotency_key is not None:
                    matches = list(media_dir.glob(f"{media_id}.*"))
                    if len(matches) > 1:
                        raise HTTPException(
                            status_code=409,
                            detail="Idempotency key maps to ambiguous Sidecar objects",
                        )
                    if matches:
                        existing = matches[0]
                        # An idempotent replay must preserve the original media
                        # extension as well as its bytes. Reusing the same key
                        # with a different suffix changes the media contract.
                        if existing.suffix.lower() != suffix:
                            raise HTTPException(
                                status_code=409,
                                detail="Idempotency key was already used for different media parameters",
                            )
                        if sha256_file(existing) != actual_sha256:
                            raise HTTPException(
                                status_code=409,
                                detail="Idempotency key was already used for different media bytes",
                            )
                        destination = existing
                        idempotent_replay = True
                        metadata = probe(existing)
                        temporary.unlink(missing_ok=True)
                    else:
                        temporary.replace(destination)
                else:
                    temporary.replace(destination)

                # The durable generation artifact is named by project/task
                # idempotency, not multipart metadata. Keep this hardening
                # strictly within the internal generation: path; regular
                # user uploads and other idempotent clients retain their API.
                canonical_generation_receipt = bool(
                    idempotency_key and idempotency_key.startswith("generation:")
                )
                generation_key_suffix = (
                    idempotency_key.removeprefix("generation:")
                    if canonical_generation_receipt else ""
                )
                # The API normally names generated files generation-{taskId}.mp4.
                # Preserve that human-readable name across replays without
                # trusting the current multipart filename; an invalid legacy
                # key falls back to the durable MediaStore filename.
                canonical_generation_name = (
                    f"generation-{generation_key_suffix}{suffix}"
                    if generation_key_suffix and IDENTIFIER.fullmatch(generation_key_suffix)
                    else destination.name
                )
                return {
                    "mediaId": media_id,
                    "projectId": projectId,
                    "fileName": (
                        canonical_generation_name if canonical_generation_receipt
                        else (filename or destination.name)
                    ),
                    "contentType": (
                        mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
                        if canonical_generation_receipt else file.content_type
                    ),
                    "mediaType": media_type,
                    "metadata": metadata,
                    "idempotentReplay": idempotent_replay,
                }
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            finally:
                await file.close()

        if idempotency_key is not None:
            # asyncio.Lock covers only this ASGI instance. A shared POSIX
            # media directory also needs an OS-released, cross-process fence
            # around the idempotency lookup and final atomic replace.
            # Locking the directory inode does not mutate stored media.
            async with upload_idempotency_lock:
                media_dir_fd = os.open(media_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    while True:
                        try:
                            fcntl.flock(media_dir_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.02)
                    try:
                        return await persist_upload()
                    finally:
                        fcntl.flock(media_dir_fd, fcntl.LOCK_UN)
                finally:
                    os.close(media_dir_fd)
        return await persist_upload()

    @app.get("/media/{project_id}/{media_id}")
    def get_media(project_id: str, media_id: str):
        try:
            source = store.find_media(project_id, media_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        return FileResponse(source, filename=source.name)

    @app.get("/media/{project_id}/{media_id}/inspect")
    def inspect_media(project_id: str, media_id: str):
        try:
            source = store.find_media(project_id, media_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        try:
            metadata = probe(source)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail="媒体文件无法通过重新探测") from exc
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        suffix = source.suffix.lower()
        content_type = "video/mp4" if suffix == ".mp4" else "application/octet-stream"
        media_type = "video" if metadata.get("video") else "audio"
        return {
            "projectId": project_id,
            "mediaId": media_id,
            "mediaType": media_type,
            "contentType": content_type,
            "sizeBytes": source.stat().st_size,
            "sha256": digest.hexdigest(),
            "metadata": metadata,
        }

    @app.post("/renders", status_code=status.HTTP_202_ACCEPTED)
    def create_render(request: RenderJobRequest):
        for item in request.ranges:
            try:
                store.find_media(request.projectId, item.mediaId)
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"未找到媒体文件：{item.mediaId}",
                ) from exc
        if request.canonicalTimeline is not None:
            for track in request.canonicalTimeline.tracks:
                if track.type == "subtitle":
                    continue
                for clip in track.clips:
                    try:
                        store.find_media(request.projectId, clip.materialId)
                    except FileNotFoundError as exc:
                        raise HTTPException(status_code=404, detail=f"未找到媒体文件：{clip.materialId}") from exc
        try:
            # The same requestId must never return a different render's output
            # after retry, restart or a lost hand-off ACK. Hash the validated
            # full render contract (including timeline, mode and audio policy).
            render_contract = json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            render_hash = hashlib.sha256(render_contract.encode("utf-8")).hexdigest()
            job, should_execute = jobs.create_or_get(
                request.projectId, "render", request.requestId, request_hash=render_hash
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not should_execute:
            return job
        try:
            pool.submit(execute_render, job, request)
        except Exception as exc:
            # The reservation may already be durable, but the hand-off outcome
            # is uncertain. Never silently reuse QUEUED as proof of execution.
            logger.exception("Render executor hand-off UNKNOWN for job %s", job["jobId"])
            try:
                jobs.update(
                    job["jobId"],
                    status="failed",
                    progress=100,
                    retryable=False,
                    message="渲染派发结果 UNKNOWN；须人工核验后再决定是否重试",
                )
            except Exception:
                logger.exception(
                    "Could not persist render hand-off UNKNOWN for job %s",
                    job["jobId"],
                )
            raise HTTPException(
                status_code=409,
                detail="Render executor hand-off UNKNOWN; manual reconciliation required",
            ) from exc
        return job

    def submit_auxiliary_or_fail_closed(job: dict[str, Any], handler, request) -> None:
        """Keep durable auxiliary jobs out of QUEUED after an uncertain hand-off."""
        try:
            pool.submit(handler, job, request)
        except Exception as exc:
            logger.exception(
                "Auxiliary %s executor hand-off UNKNOWN for job %s",
                job["kind"], job["jobId"],
            )
            try:
                jobs.update(
                    job["jobId"],
                    status="failed",
                    progress=100,
                    retryable=False,
                    message="Sidecar任务派发结果 UNKNOWN；须人工核验后再决定是否重试",
                )
            except Exception:
                logger.exception(
                    "Could not persist auxiliary hand-off UNKNOWN for job %s",
                    job["jobId"],
                )
            raise HTTPException(
                status_code=409,
                detail="Sidecar executor hand-off UNKNOWN; manual reconciliation required",
            ) from exc

    @app.post("/transcriptions", status_code=status.HTTP_202_ACCEPTED)
    def create_transcription(request: TranscriptionJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        job = jobs.create(request.projectId, "transcription")
        submit_auxiliary_or_fail_closed(job, execute_transcription, request)
        return job

    @app.post("/timeline-views", status_code=status.HTTP_202_ACCEPTED)
    def create_timeline_view(request: TimelineViewJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        job = jobs.create(request.projectId, "timeline-view")
        submit_auxiliary_or_fail_closed(job, execute_timeline_view, request)
        return job

    def require_completed_render_bytes(job: dict[str, Any]) -> None:
        """Fail closed when a completed render no longer has its frozen bytes.

        This guard runs at read time (including restart) before any Worker may
        convert Sidecar 'completed' into API READY. It is not a signed receipt:
        the Sidecar itself remains the observer, and a later mutation is still
        possible. Older terminal records without proof require reconciliation.
        """
        if job.get("kind") != "render" or job.get("status") != "completed":
            return
        message = "Completed render byte proof unavailable; manual reconciliation required"
        project_id, job_id = job.get("projectId"), job.get("jobId")
        digest, size = job.get("artifactSha256"), job.get("artifactSizeBytes")
        if not isinstance(project_id, str) or not isinstance(job_id, str):
            raise HTTPException(status_code=409, detail=message)
        try:
            validate_identifier(project_id, "projectId")
            validate_identifier(job_id, "jobId")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=message) from exc
        expected = f"projects/{project_id}/renders/{job_id}/final.mp4"
        if (
            job.get("artifact") != expected
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise HTTPException(status_code=409, detail=message)
        candidate = store.root / expected
        try:
            # Reject symlink aliases even when they stay within media_root.
            # An accepted string path must identify the original owned inode.
            resolved = candidate.resolve(strict=True)
            if resolved != candidate.absolute() or not resolved.is_file():
                raise ValueError("render artifact path is missing or aliased")
            if resolved.stat().st_size != size or sha256_file(resolved) != digest:
                raise ValueError("render artifact size/hash changed")
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=message) from exc

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            validate_identifier(job_id, "jobId")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="未找到该任务") from exc
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        require_completed_render_bytes(job)
        return job

    @app.get("/jobs/{job_id}/artifact")
    def get_job_artifact(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.get("status") != "completed" or not job.get("artifact"):
            raise HTTPException(status_code=404, detail="成片文件尚不可用")
        require_completed_render_bytes(job)
        artifact = (store.root / job["artifact"]).resolve()
        try:
            artifact.relative_to(store.root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="成片文件尚不可用") from exc
        if not artifact.is_file():
            raise HTTPException(status_code=404, detail="成片文件尚不可用")
        return FileResponse(artifact, filename=artifact.name)

    return app


app = create_app()
