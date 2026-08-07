import asyncio
import datetime
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Dict, Generator, List

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import DBProject
from .schemas import (
    CreateProjectRequest,
    ProjectResponse,
    UpdateProjectRequest,
    MoneyPrinterGenerateRequest,
)
from .moneyprinter_adapter import MoneyPrinterTurboAdapter
from .video_use_adapter import VideoUseAdapter, VideoUseError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.main")

DatabaseDependency = Callable[[], Generator[Session, None, None]]


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(value: datetime.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def project_response(project: DBProject) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        timeline=project.timeline,
        materials=project.materials,
        revision=project.revision,
        createdAt=iso_utc(project.created_at),
        updatedAt=iso_utc(project.updated_at),
    )


def apply_project_update(
    db: Session,
    project_id: str,
    req: UpdateProjectRequest,
) -> DBProject:
    values = {
        "revision": req.expectedRevision + 1,
        "updated_at": utc_now(),
    }
    if req.name is not None:
        values["name"] = req.name
    if req.timeline is not None:
        values["timeline"] = req.timeline.model_dump()
    if req.materials is not None:
        values["materials"] = [material.model_dump() for material in req.materials]

    result = db.execute(
        update(DBProject)
        .where(
            DBProject.id == project_id,
            DBProject.revision == req.expectedRevision,
        )
        .values(**values)
    )

    if result.rowcount != 1:
        db.rollback()
        exists = db.execute(
            select(DBProject.id).where(DBProject.id == project_id)
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PROJECT_NOT_FOUND",
                    "message": f"Project {project_id} not found",
                },
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONCURRENCY_CONFLICT",
                "message": (
                    f"Revision conflict: expected {req.expectedRevision}; "
                    "the project has already changed"
                ),
            },
        )

    db.commit()
    return db.execute(
        select(DBProject).where(DBProject.id == project_id)
    ).scalar_one()


def create_app(
    app_engine: Engine = engine,
    db_dependency: DatabaseDependency = get_db,
    render_step_delay: float = 1.0,
    video_use_adapter: VideoUseAdapter | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        Base.metadata.create_all(bind=app_engine)
        yield

    created_app = FastAPI(
        title="Aether Studio API",
        version="1.0.0",
        lifespan=lifespan,
    )
    created_app.state.active_tasks = {}
    created_app.state.moneyprinter = MoneyPrinterTurboAdapter()
    created_app.state.video_use = video_use_adapter or VideoUseAdapter()

    origins = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ]
    created_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @created_app.get("/health")
    def health_check(db: Session = Depends(db_dependency)):
        journal_mode = db.execute(text("PRAGMA journal_mode;")).scalar_one().upper()
        return {
            "status": "healthy",
            "service": "api",
            "database": "sqlite",
            "journal_mode": journal_mode,
            "timestamp": iso_utc(utc_now()),
        }

    @created_app.get("/moneyprinter/health")
    def moneyprinter_health():
        adapter: MoneyPrinterTurboAdapter = created_app.state.moneyprinter
        return adapter.check_health()

    @created_app.get("/moneyprinter/capabilities")
    def moneyprinter_capabilities():
        adapter: MoneyPrinterTurboAdapter = created_app.state.moneyprinter
        return adapter.get_capabilities()

    @created_app.post("/moneyprinter/generate")
    def moneyprinter_generate(req: MoneyPrinterGenerateRequest):
        adapter: MoneyPrinterTurboAdapter = created_app.state.moneyprinter
        try:
            task_id = adapter.generate_video(
                subject=req.video_subject,
                aspect=req.video_aspect,
                voice_name=req.voice_name,
                video_concat_mode=req.video_concat_mode,
                video_clip_duration=req.video_clip_duration,
            )
            return {"task_id": task_id, "status": "submitted"}
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "MONEYPRINTER_API_ERROR",
                    "message": f"Failed to submit task to MoneyPrinterTurbo sidecar: {exc}",
                }
            )

    @created_app.get("/moneyprinter/status/{task_id}")
    def moneyprinter_status(task_id: str):
        adapter: MoneyPrinterTurboAdapter = created_app.state.moneyprinter
        try:
            return adapter.get_task_status(task_id)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "MONEYPRINTER_STATUS_ERROR",
                    "message": f"Failed to query task status from MoneyPrinterTurbo sidecar: {exc}",
                }
            )

    @created_app.get("/video-use/health")
    def video_use_health():
        adapter: VideoUseAdapter = created_app.state.video_use
        return adapter.check_health()

    @created_app.get("/video-use/capabilities")
    def video_use_capabilities():
        adapter: VideoUseAdapter = created_app.state.video_use
        try:
            return adapter.get_capabilities()
        except VideoUseError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "VIDEO_USE_UNAVAILABLE", "message": str(exc)},
            ) from exc

    @created_app.get("/projects", response_model=List[ProjectResponse])
    def list_projects(db: Session = Depends(db_dependency)):
        projects = db.execute(
            select(DBProject).order_by(DBProject.created_at.asc())
        ).scalars()
        return [project_response(project) for project in projects]

    @created_app.post(
        "/projects",
        response_model=ProjectResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def create_project(
        req: CreateProjectRequest,
        db: Session = Depends(db_dependency),
    ):
        now = utc_now()
        db_project = DBProject(
            id=str(uuid.uuid4()),
            name=req.name,
            timeline={"version": "1.1", "tracks": []},
            materials=[],
            revision=1,
            created_at=now,
            updated_at=now,
        )
        db.add(db_project)
        db.commit()
        db.refresh(db_project)
        return project_response(db_project)

    @created_app.get("/projects/{project_id}", response_model=ProjectResponse)
    def get_project(project_id: str, db: Session = Depends(db_dependency)):
        project = db.execute(
            select(DBProject).where(DBProject.id == project_id)
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PROJECT_NOT_FOUND",
                    "message": f"Project {project_id} not found",
                },
            )
        return project_response(project)

    @created_app.put("/projects/{project_id}", response_model=ProjectResponse)
    def update_project(
        project_id: str,
        req: UpdateProjectRequest,
        db: Session = Depends(db_dependency),
    ):
        return project_response(apply_project_update(db, project_id, req))

    @created_app.post("/projects/{project_id}/media", status_code=status.HTTP_201_CREATED)
    def upload_project_media(
        project_id: str,
        expectedRevision: int = Form(..., ge=0),
        file: UploadFile = File(...),
        db: Session = Depends(db_dependency),
    ):
        project = db.execute(
            select(DBProject).where(DBProject.id == project_id)
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PROJECT_NOT_FOUND", "message": f"Project {project_id} not found"},
            )
        if project.revision != expectedRevision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONCURRENCY_CONFLICT",
                    "message": f"Revision conflict: expected {expectedRevision}",
                },
            )

        adapter: VideoUseAdapter = created_app.state.video_use
        try:
            uploaded = adapter.upload_media(
                project_id,
                file.filename or "media.mp4",
                file.content_type,
                file.file,
            )
        except VideoUseError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "VIDEO_USE_UPLOAD_FAILED", "message": str(exc)},
            ) from exc
        finally:
            file.file.close()

        metadata = uploaded.get("metadata") or {}
        duration_seconds = float(metadata.get("durationSeconds") or 0)
        media_type = "video" if metadata.get("video") else "audio"
        media_id = uploaded["mediaId"]
        material = {
            "id": media_id,
            "name": uploaded.get("fileName") or file.filename or media_id,
            "url": f"/api/video-use/media/{project_id}/{media_id}",
            "type": media_type,
            "duration": {
                "value": max(1, round(duration_seconds * 24_000)),
                "timescale": 24_000,
            },
        }
        materials = [*project.materials, material]
        updated = apply_project_update(
            db,
            project_id,
            UpdateProjectRequest(materials=materials, expectedRevision=expectedRevision),
        )
        return {"material": material, "project": project_response(updated)}

    def proxy_video_use_stream(path: str):
        adapter: VideoUseAdapter = created_app.state.video_use

        def body():
            with adapter.stream(path) as response:
                yield from response.iter_bytes()

        return StreamingResponse(body(), media_type="application/octet-stream")

    @created_app.get("/video-use/media/{project_id}/{media_id}")
    def stream_project_media(project_id: str, media_id: str):
        return proxy_video_use_stream(f"/media/{project_id}/{media_id}")

    @created_app.post("/projects/{project_id}/render")
    def start_render_task(
        project_id: str,
        db: Session = Depends(db_dependency),
    ):
        project = db.execute(
            select(DBProject).where(DBProject.id == project_id)
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PROJECT_NOT_FOUND",
                    "message": f"Project {project_id} not found",
                },
            )

        materials = {material["id"]: material for material in project.materials}
        ordered_clips: list[tuple[float, dict, dict]] = []
        for track in project.timeline.get("tracks", []):
            if track.get("type") != "video":
                continue
            for clip in track.get("clips", []):
                material = materials.get(clip.get("materialId"))
                if material is None or material.get("type") != "video":
                    continue
                timeline_start = clip["start"]["value"] / clip["start"]["timescale"]
                ordered_clips.append((timeline_start, clip, material))

        if not ordered_clips:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "NO_RENDERABLE_VIDEO",
                    "message": "Upload a video and place it on a video track before rendering",
                },
            )

        ranges = []
        for _timeline_start, clip, material in sorted(ordered_clips, key=lambda value: value[0]):
            source_in = clip["sourceIn"]["value"] / clip["sourceIn"]["timescale"]
            duration = clip["duration"]["value"] / clip["duration"]["timescale"]
            ranges.append(
                {
                    "mediaId": material["id"],
                    "start": source_in,
                    "end": source_in + duration,
                    "note": clip.get("id"),
                }
            )

        adapter: VideoUseAdapter = created_app.state.video_use
        try:
            upstream = adapter.submit_render(
                {
                    "projectId": project_id,
                    "ranges": ranges,
                    "mode": "preview",
                    "grade": "auto",
                    "normalizeAudio": True,
                }
            )
        except VideoUseError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "VIDEO_USE_RENDER_FAILED", "message": str(exc)},
            ) from exc

        task_id = upstream["jobId"]
        active_tasks: Dict[str, dict] = created_app.state.active_tasks
        active_tasks[task_id] = {
            "taskId": task_id,
            "projectId": project_id,
            "progress": upstream.get("progress", 0),
            "status": upstream.get("status", "queued"),
            "message": upstream.get("message", "Queued in video-use"),
            "engine": "video-use",
        }
        return {"taskId": task_id, "status": upstream.get("status", "queued"), "mock": False}

    @created_app.get("/renders/{task_id}/artifact")
    def stream_render_artifact(task_id: str):
        return proxy_video_use_stream(f"/jobs/{task_id}/artifact")

    @created_app.get("/events")
    async def sse_events(
        once: bool = Query(
            False,
            description="Return one bounded event snapshot; useful for health tests.",
        )
    ):
        async def event_generator():
            while True:
                heartbeat = {"timestamp": iso_utc(utc_now())}
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat)}\n\n"

                active_tasks: Dict[str, dict] = created_app.state.active_tasks
                for task_id, task in list(active_tasks.items()):
                    if task["status"] not in {"completed", "failed"}:
                        adapter: VideoUseAdapter = created_app.state.video_use
                        try:
                            upstream = adapter.get_job_status(task_id)
                            task.update(
                                progress=upstream.get("progress", task["progress"]),
                                status=upstream.get("status", task["status"]),
                                message=upstream.get("message", task["message"]),
                            )
                            if task["status"] == "completed":
                                task["artifactUrl"] = f"/api/renders/{task_id}/artifact"
                        except VideoUseError as exc:
                            logger.warning("Unable to refresh video-use job %s: %s", task_id, exc)
                    yield (
                        "event: task_progress\n"
                        f"data: {json.dumps(task, separators=(',', ':'))}\n\n"
                    )
                    if task["status"] in {"completed", "failed"}:
                        active_tasks.pop(task_id, None)

                if once:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return created_app


app = create_app()
