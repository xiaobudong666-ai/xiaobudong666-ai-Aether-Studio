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
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
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


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        (root / "jobs").mkdir(parents=True, exist_ok=True)
        for path in (root / "jobs").glob("*.json"):
            try:
                payload = json.loads(path.read_text())
                if payload.get("status") in {"queued", "processing"}:
                    payload.update(
                        status="failed",
                        progress=100,
                        message="渲染服务重启导致任务中断，可以安全重试",
                        retryable=True,
                        updatedAt=utc_now(),
                    )
                    self._persist(payload)
                self._jobs[payload["jobId"]] = payload
            except (OSError, ValueError, KeyError):
                logger.warning("Ignoring invalid persisted job file %s", path)

    def create(self, project_id: str, kind: str, job_id: str | None = None) -> dict[str, Any]:
        payload, _should_execute = self.create_or_get(project_id, kind, job_id)
        return payload

    def create_or_get(
        self,
        project_id: str,
        kind: str,
        job_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        job_id = job_id or str(uuid.uuid4())
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                if existing.get("projectId") != project_id or existing.get("kind") != kind:
                    raise ValueError("requestId is already bound to another job")
                if existing.get("status") != "failed" or not existing.get("retryable"):
                    return dict(existing), False
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
            }
            self._jobs[job_id] = payload
            self._persist(payload)
        return dict(payload), True

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
            jobs.update(
                job_id,
                status="completed",
                progress=100,
                message="成片渲染完成",
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
    ):
        try:
            validate_identifier(projectId, "projectId")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=415, detail="不支持该媒体文件扩展名")
        media_id = str(uuid.uuid4())
        destination = store.media_dir(projectId) / f"{media_id}{suffix}"
        size = 0
        try:
            with destination.open("wb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="媒体文件过大")
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
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        return FileResponse(source, filename=source.name)

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
            job, should_execute = jobs.create_or_get(request.projectId, "render", request.requestId)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not should_execute:
            return job
        pool.submit(execute_render, job, request)
        return job

    @app.post("/transcriptions", status_code=status.HTTP_202_ACCEPTED)
    def create_transcription(request: TranscriptionJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        job = jobs.create(request.projectId, "transcription")
        pool.submit(execute_transcription, job, request)
        return job

    @app.post("/timeline-views", status_code=status.HTTP_202_ACCEPTED)
    def create_timeline_view(request: TimelineViewJobRequest):
        try:
            store.find_media(request.projectId, request.mediaId)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="未找到该媒体文件") from exc
        job = jobs.create(request.projectId, "timeline-view")
        pool.submit(execute_timeline_view, job, request)
        return job

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            validate_identifier(job_id, "jobId")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="未找到该任务") from exc
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="未找到该任务")
        return job

    @app.get("/jobs/{job_id}/artifact")
    def get_job_artifact(job_id: str):
        job = jobs.get(job_id)
        if job is None or job.get("status") != "completed" or not job.get("artifact"):
            raise HTTPException(status_code=404, detail="成片文件尚不可用")
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
