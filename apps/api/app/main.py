import asyncio
import datetime
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Callable, Dict, Generator, List

from fastapi import Depends, FastAPI, HTTPException, Query, status
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

    @created_app.post("/projects/{project_id}/render")
    async def start_render_task(
        project_id: str,
        db: Session = Depends(db_dependency),
    ):
        project_exists = db.execute(
            select(DBProject.id).where(DBProject.id == project_id)
        ).scalar_one_or_none()
        if project_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "PROJECT_NOT_FOUND",
                    "message": f"Project {project_id} not found",
                },
            )

        task_id = str(uuid.uuid4())
        active_tasks: Dict[str, dict] = created_app.state.active_tasks
        active_tasks[task_id] = {
            "taskId": task_id,
            "projectId": project_id,
            "progress": 0,
            "status": "pending",
            "message": "Initializing background render task [MOCK]",
        }

        async def simulate_task():
            steps = (
                (20, "processing", "Processing timeline frames... [MOCK]"),
                (50, "processing", "Generating 480p proxy with FFmpeg... [MOCK]"),
                (80, "processing", "Merging audio layers... [MOCK]"),
                (100, "completed", "Render successfully completed [MOCK]"),
            )
            try:
                for progress, task_status, message in steps:
                    await asyncio.sleep(render_step_delay)
                    if task_id not in active_tasks:
                        return
                    active_tasks[task_id].update(
                        progress=progress,
                        status=task_status,
                        message=message,
                    )
            except Exception as exc:  # pragma: no cover - defensive boundary
                logger.exception("Mock render task failed")
                if task_id in active_tasks:
                    active_tasks[task_id].update(
                        status="failed",
                        message=f"Task failed: {exc} [MOCK]",
                    )

        asyncio.create_task(simulate_task())
        return {"taskId": task_id, "status": "pending", "mock": True}

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
