from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

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
}
MAX_UPLOAD_BYTES = int(os.environ.get("VIDEO_USE_MAX_UPLOAD_BYTES", str(2 * 1024**3)))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} contains unsupported characters")
    return value


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
            raise ValueError("end must be greater than start")
        return value


class RenderJobRequest(BaseModel):
    projectId: str
    ranges: list[RenderRange] = Field(min_length=1, max_length=500)
    mode: Literal["draft", "preview", "final"] = "preview"
    grade: Literal["auto", "subtle", "neutral_punch", "warm_cinematic"] = "auto"
    normalizeAudio: bool = True

    @field_validator("projectId")
    @classmethod
    def project_id_is_safe(cls, value: str) -> str:
        return validate_identifier(value, "projectId")


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
            raise ValueError("end must be greater than start")
        return value


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        (root / "jobs").mkdir(parents=True, exist_ok=True)

    def create(self, project_id: str, kind: str) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        payload = {
            "jobId": job_id,
            "projectId": project_id,
            "kind": kind,
            "status": "queued",
            "progress": 0,
            "message": "Queued",
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "artifact": None,
        }
        with self._lock:
            self._jobs[job_id] = payload
            self._persist(payload)
        return dict(payload)

    def update(self, job_id: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            payload = self._jobs[job_id]
            payload.update(values, updatedAt=utc_now())
            self._persist(payload)
            return dict(payload)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            payload = self._jobs.get(job_id)
            if payload is not None:
                return dict(payload)
        path = self.root / "jobs" / f"{job_id}.json"
        if not path.is_file():
            return None
        payload = json.loads(path.read_text())
        with self._lock:
            self._jobs[job_id] = payload
        return dict(payload)

    def _persist(self, payload: dict[str, Any]) -> None:
        target = self.root / "jobs" / f"{payload['jobId']}.json"
        temporary = target.with_suffix(".json.pending")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        temporary.replace(target)


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
        return subprocess.run(
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
    root = Path(media_root or os.environ.get("VIDEO_USE_MEDIA_ROOT", "/media"))
    upstream = Path(upstream_root or os.environ.get("VIDEO_USE_UPSTREAM_ROOT", "/opt/video-use"))
    store = MediaStore(root)
    jobs = JobStore(root)
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
        jobs.update(job_id, status="failed", progress=100, message=message[-2_000:])

    def execute_render(job: dict[str, Any], request: RenderJobRequest) -> None:
        job_id = job["jobId"]
        try:
            jobs.update(job_id, status="processing", progress=10, message="Preparing EDL")
            job_dir = store.job_dir(request.projectId, job_id)
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
                ranges.append(
                    {
                        "source": alias,
                        "start": item.start,
                        "end": item.end,
                        "note": item.note or "",
                    }
                )

            edl = {"sources": source_aliases, "ranges": ranges, "grade": request.grade}
            edl_path = job_dir / "edl.json"
            edl_path.write_text(json.dumps(edl, ensure_ascii=False, indent=2))
            output = job_dir / "final.mp4"
            command = [sys.executable, str(helper("render.py")), str(edl_path), "-o", str(output)]
            if request.mode == "draft":
                command.append("--draft")
            elif request.mode == "preview":
                command.append("--preview")
            command.append("--no-subtitles")
            if not request.normalizeAudio:
                command.append("--no-loudnorm")

            jobs.update(job_id, progress=25, message="Rendering with pinned video-use")
            run_checked(command)
            metadata = probe(output)
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="Render completed",
                artifact=str(output.relative_to(store.root)),
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
            jobs.update(job_id, status="processing", progress=20, message="Transcribing with Scribe")
            run_checked(command, timeout=1_800)
            transcript = project_dir / "edit" / "transcripts" / f"{source.stem}.json"
            if not transcript.is_file():
                raise RuntimeError("Transcription completed without an artifact")
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="Transcription completed",
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
            jobs.update(job_id, status="processing", progress=20, message="Building timeline view")
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
                message="Timeline view completed",
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
    ):
        try:
            validate_identifier(projectId, "projectId")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=415, detail="Unsupported media extension")
        media_id = str(uuid.uuid4())
        destination = store.media_dir(projectId) / f"{media_id}{suffix}"
        size = 0
        try:
            with destination.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Media file is too large")
                    output.write(chunk)
            metadata = probe(destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        return {
            "mediaId": media_id,
            "projectId": projectId,
            "fileName": Path(file.filename or destination.name).name,
            "contentType": file.content_type,
            "metadata": metadata,
        }

    @app.get("/media/{project_id}/{media_id}")
    def get_media(project_id: str, media_id: str):
        try:
            source = store.find_media(project_id, media_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Media not found") from exc
        return FileResponse(source, filename=source.name)

    @app.post("/renders", status_code=status.HTTP_202_ACCEPTED)
    def create_render(request: RenderJobRequest):
        for item in request.ranges:
            try:
                store.find_media(request.projectId, item.mediaId)
            except FileNotFoundError as exc:
                raise HTTPException(
                    status_code=404,
                    detail=f"Media not found: {item.mediaId}",
                ) from exc
        job = jobs.create(request.projectId, "render")
        pool.submit(execute_render, job, request)
        return job

    @app.post("/transcriptions", status_code=status.HTTP_202_ACCEPTED)
    def create_transcription(request: TranscriptionJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Media not found") from exc
        job = jobs.create(request.projectId, "transcription")
        pool.submit(execute_transcription, job, request)
        return job

    @app.post("/timeline-views", status_code=status.HTTP_202_ACCEPTED)
    def create_timeline_view(request: TimelineViewJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Media not found") from exc
        job = jobs.create(request.projectId, "timeline-view")
        pool.submit(execute_timeline_view, job, request)
        return job

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            validate_identifier(job_id, "jobId")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    @app.get("/jobs/{job_id}/artifact")
    def get_job_artifact(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.get("status") != "completed" or not job.get("artifact"):
            raise HTTPException(status_code=404, detail="Artifact not available")
        artifact = (store.root / job["artifact"]).resolve()
        try:
            artifact.relative_to(store.root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Artifact not available") from exc
        if not artifact.is_file():
            raise HTTPException(status_code=404, detail="Artifact not available")
        return FileResponse(artifact, filename=artifact.name)

    return app


app = create_app()
