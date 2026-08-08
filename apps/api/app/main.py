from __future__ import annotations

import asyncio
import datetime
import hmac
import json
import logging
import os
import uuid
from collections.abc import Callable, Generator
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .auth import (
    SESSION_COOKIE,
    AuthContext,
    bootstrap_identity,
    clear_session_cookie,
    create_session,
    hash_password,
    normalize_email,
    public_user,
    require_auth,
    require_roles,
    set_session_cookie,
    token_digest,
    verify_password,
)
from .database import engine, get_db
from .migrations import ensure_schema
from .models import DBExternalTask, DBProject, DBRenderTask, DBSession, DBTenant, DBUser
from .moneyprinter_adapter import MoneyPrinterTurboAdapter
from .schemas import (
    CreateProjectRequest,
    CreateUserRequest,
    LoginRequest,
    MoneyPrinterGenerateRequest,
    ProjectResponse,
    UpdateProjectRequest,
    WorkerTaskUpdateRequest,
)
from .timeline_render import build_render_payload
from .video_use_adapter import VideoUseAdapter, VideoUseError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.main")

DatabaseDependency = Callable[[], Generator[Session, None, None]]
ACTIVE_TASK_STATES = {"queued", "dispatching", "processing"}


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


def task_response(task: DBRenderTask) -> dict:
    payload = {
        "taskId": task.id,
        "projectId": task.project_id,
        "progress": task.progress,
        "status": task.status,
        "message": task.message,
        "engine": task.engine,
        "attempts": task.attempts,
        "createdAt": iso_utc(task.created_at),
        "updatedAt": iso_utc(task.updated_at),
    }
    if task.status == "completed":
        payload["artifactUrl"] = f"/api/renders/{task.id}/artifact"
    if task.error:
        payload["error"] = task.error
    return payload


def apply_project_update(
    db: Session,
    project_id: str,
    req: UpdateProjectRequest,
    tenant_id: str | None = None,
    allow_materials: bool = False,
) -> DBProject:
    if req.materials is not None and not allow_materials:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MATERIALS_SERVER_MANAGED",
                "message": "Project materials can only be changed through the media API",
            },
        )
    values = {"revision": req.expectedRevision + 1, "updated_at": utc_now()}
    if req.name is not None:
        values["name"] = req.name
    if req.timeline is not None:
        values["timeline"] = req.timeline.model_dump()
    if req.materials is not None:
        values["materials"] = [material.model_dump() for material in req.materials]

    predicates = [DBProject.id == project_id, DBProject.revision == req.expectedRevision]
    if tenant_id is not None:
        predicates.append(DBProject.tenant_id == tenant_id)
    result = db.execute(update(DBProject).where(*predicates).values(**values))
    if result.rowcount != 1:
        db.rollback()
        exists_query = select(DBProject.id).where(DBProject.id == project_id)
        if tenant_id is not None:
            exists_query = exists_query.where(DBProject.tenant_id == tenant_id)
        exists = db.execute(exists_query).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PROJECT_NOT_FOUND", "message": f"Project {project_id} not found"},
            )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONCURRENCY_CONFLICT",
                "message": f"Revision conflict: expected {req.expectedRevision}; the project has already changed",
            },
        )
    db.commit()
    query = select(DBProject).where(DBProject.id == project_id)
    if tenant_id is not None:
        query = query.where(DBProject.tenant_id == tenant_id)
    return db.execute(query).scalar_one()


def create_app(
    app_engine: Engine = engine,
    db_dependency: DatabaseDependency = get_db,
    render_step_delay: float = 1.0,
    video_use_adapter: VideoUseAdapter | None = None,
    bootstrap_admin_password: str | None = None,
    bootstrap_admin_email: str | None = None,
    worker_token: str | None = None,
    cookie_secure: bool | None = None,
    enforce_csrf: bool | None = None,
) -> FastAPI:
    del render_step_delay  # retained for backwards-compatible test construction
    internal_sessions = sessionmaker(autocommit=False, autoflush=False, bind=app_engine)
    resolved_worker_token = worker_token if worker_token is not None else os.environ.get("AETHER_WORKER_TOKEN", "")
    resolved_cookie_secure = cookie_secure if cookie_secure is not None else os.environ.get("AETHER_COOKIE_SECURE", "true").lower() == "true"
    resolved_enforce_csrf = enforce_csrf if enforce_csrf is not None else os.environ.get("AETHER_ENFORCE_CSRF", "true").lower() == "true"
    session_hours = max(1, int(os.environ.get("AETHER_SESSION_HOURS", "12")))
    lease_seconds = max(15, int(os.environ.get("AETHER_TASK_LEASE_SECONDS", "60")))
    max_upload_bytes = int(os.environ.get("AETHER_MAX_UPLOAD_BYTES", str(2 * 1024**3)))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ensure_schema(app_engine)
        with internal_sessions() as db:
            bootstrap = bootstrap_identity(
                db,
                utc_now(),
                password_override=bootstrap_admin_password,
                email_override=bootstrap_admin_email,
            )
            if bootstrap is not None:
                db.execute(
                    update(DBProject)
                    .where(DBProject.tenant_id.is_(None))
                    .values(tenant_id=bootstrap.tenant_id, owner_id=bootstrap.id)
                )
                db.commit()
            _app.state.setup_required = bootstrap is None
        yield

    created_app = FastAPI(title="Aether Studio API", version="1.1.0", lifespan=lifespan)
    created_app.state.moneyprinter = MoneyPrinterTurboAdapter()
    created_app.state.video_use = video_use_adapter or VideoUseAdapter()
    created_app.state.setup_required = False

    origins = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    ]
    created_app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @created_app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if (
            resolved_enforce_csrf
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path not in {"/auth/login"}
            and request.cookies.get(SESSION_COOKIE)
            and request.headers.get("x-aether-csrf") != "1"
        ):
            return Response(
                content=json.dumps({"detail": {"code": "CSRF_REQUIRED", "message": "Missing same-origin request proof"}}),
                status_code=403,
                media_type="application/json",
            )
        return await call_next(request)

    def context_dependency(request: Request, db: Session = Depends(db_dependency)) -> AuthContext:
        return require_auth(request, db, utc_now())

    def project_for_tenant(db: Session, project_id: str, context: AuthContext) -> DBProject:
        project = db.execute(
            select(DBProject).where(
                DBProject.id == project_id,
                DBProject.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PROJECT_NOT_FOUND", "message": f"Project {project_id} not found"},
            )
        return project

    def refresh_quota_period(db: Session, tenant: DBTenant) -> bool:
        period = utc_now().strftime("%Y-%m")
        if tenant.quota_period != period:
            tenant.quota_period = period
            tenant.render_seconds_used = 0
            tenant.updated_at = utc_now()
            return True
        return False

    def begin_serialized_write(db: Session) -> None:
        if app_engine.dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))

    def require_internal_token(x_worker_token: str | None) -> None:
        if not resolved_worker_token or not x_worker_token or not hmac.compare_digest(resolved_worker_token, x_worker_token):
            raise HTTPException(status_code=401, detail={"code": "WORKER_AUTH_FAILED", "message": "Invalid worker token"})

    @created_app.get("/health")
    def health_check(db: Session = Depends(db_dependency)):
        journal_mode = db.execute(text("PRAGMA journal_mode;")).scalar_one().upper()
        return {
            "status": "healthy",
            "service": "api",
            "database": "sqlite",
            "journal_mode": journal_mode,
            "setupRequired": created_app.state.setup_required,
            "timestamp": iso_utc(utc_now()),
        }

    @created_app.post("/auth/login")
    def login(req: LoginRequest, response: Response, db: Session = Depends(db_dependency)):
        user = db.execute(select(DBUser).where(DBUser.email == normalize_email(req.email))).scalar_one_or_none()
        if user is None or not user.is_active or not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Email or password is incorrect"})
        tenant = db.execute(select(DBTenant).where(DBTenant.id == user.tenant_id)).scalar_one()
        _session, token = create_session(db, user, utc_now(), session_hours)
        set_session_cookie(response, token, session_hours, resolved_cookie_secure)
        return public_user(user, tenant)

    @created_app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response, db: Session = Depends(db_dependency)):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            session = db.execute(select(DBSession).where(DBSession.token_hash == token_digest(token))).scalar_one_or_none()
            if session is not None:
                db.delete(session)
                db.commit()
        clear_session_cookie(response, resolved_cookie_secure)
        response.status_code = 204
        return response

    @created_app.get("/auth/me")
    def auth_me(context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        user = db.execute(select(DBUser).where(DBUser.id == context.user_id)).scalar_one()
        tenant = db.execute(select(DBTenant).where(DBTenant.id == context.tenant_id)).scalar_one()
        if refresh_quota_period(db, tenant):
            db.commit()
        return public_user(user, tenant)

    @created_app.get("/admin/users")
    def list_users(context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner")
        users = db.execute(select(DBUser).where(DBUser.tenant_id == context.tenant_id).order_by(DBUser.created_at)).scalars()
        return [{"id": user.id, "email": user.email, "displayName": user.display_name, "role": user.role, "isActive": user.is_active} for user in users]

    @created_app.post("/admin/users", status_code=201)
    def create_user(req: CreateUserRequest, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner")
        email = normalize_email(req.email)
        if db.execute(select(DBUser.id).where(DBUser.email == email)).scalar_one_or_none():
            raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "A user with this email already exists"})
        now = utc_now()
        user = DBUser(
            id=str(uuid.uuid4()), tenant_id=context.tenant_id, email=email,
            display_name=req.displayName.strip(), password_hash=hash_password(req.password),
            role=req.role, is_active=True, created_at=now, updated_at=now,
        )
        db.add(user)
        db.commit()
        return {"id": user.id, "email": user.email, "displayName": user.display_name, "role": user.role, "isActive": True}

    @created_app.get("/moneyprinter/health")
    def moneyprinter_health():
        return created_app.state.moneyprinter.check_health()

    @created_app.get("/moneyprinter/capabilities")
    def moneyprinter_capabilities():
        return created_app.state.moneyprinter.get_capabilities()

    @created_app.post("/moneyprinter/generate")
    def moneyprinter_generate(req: MoneyPrinterGenerateRequest, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner", "editor")
        try:
            task_id = created_app.state.moneyprinter.generate_video(
                subject=req.video_subject, aspect=req.video_aspect, voice_name=req.voice_name,
                video_concat_mode=req.video_concat_mode, video_clip_duration=req.video_clip_duration,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"code": "MONEYPRINTER_API_ERROR", "message": f"Failed to submit task to MoneyPrinterTurbo sidecar: {exc}"}) from exc
        now = utc_now()
        db.add(DBExternalTask(id=task_id, tenant_id=context.tenant_id, requested_by=context.user_id, engine="moneyprinter", status="submitted", created_at=now, updated_at=now))
        db.commit()
        return {"task_id": task_id, "status": "submitted"}

    @created_app.get("/moneyprinter/status/{task_id}")
    def moneyprinter_status(task_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        task = db.execute(select(DBExternalTask).where(DBExternalTask.id == task_id, DBExternalTask.tenant_id == context.tenant_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Task not found"})
        try:
            payload = created_app.state.moneyprinter.get_task_status(task_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail={"code": "MONEYPRINTER_STATUS_ERROR", "message": f"Failed to query task status: {exc}"}) from exc
        task.status = str(payload.get("status", task.status))
        task.updated_at = utc_now()
        db.commit()
        return payload

    @created_app.get("/video-use/health")
    def video_use_health():
        return created_app.state.video_use.check_health()

    @created_app.get("/video-use/capabilities")
    def video_use_capabilities():
        try:
            return created_app.state.video_use.get_capabilities()
        except VideoUseError as exc:
            raise HTTPException(status_code=502, detail={"code": "VIDEO_USE_UNAVAILABLE", "message": str(exc)}) from exc

    @created_app.get("/projects", response_model=list[ProjectResponse])
    def list_projects(context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        projects = db.execute(select(DBProject).where(DBProject.tenant_id == context.tenant_id).order_by(DBProject.created_at.asc())).scalars()
        return [project_response(project) for project in projects]

    @created_app.post("/projects", response_model=ProjectResponse, status_code=201)
    def create_project(req: CreateProjectRequest, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner", "editor")
        begin_serialized_write(db)
        tenant_query = select(DBTenant).where(DBTenant.id == context.tenant_id)
        if app_engine.dialect.name != "sqlite":
            tenant_query = tenant_query.with_for_update()
        tenant = db.execute(tenant_query).scalar_one()
        count = db.execute(select(func.count(DBProject.id)).where(DBProject.tenant_id == context.tenant_id)).scalar_one()
        if count >= tenant.project_quota:
            raise HTTPException(status_code=429, detail={"code": "PROJECT_QUOTA_EXCEEDED", "message": "Project quota reached"})
        now = utc_now()
        project = DBProject(
            id=str(uuid.uuid4()), tenant_id=context.tenant_id, owner_id=context.user_id,
            name=req.name.strip(), timeline={"version": "1.1", "tracks": []}, materials=[],
            revision=1, created_at=now, updated_at=now,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return project_response(project)

    @created_app.get("/projects/{project_id}", response_model=ProjectResponse)
    def get_project(project_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        return project_response(project_for_tenant(db, project_id, context))

    @created_app.put("/projects/{project_id}", response_model=ProjectResponse)
    def update_project(project_id: str, req: UpdateProjectRequest, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner", "editor")
        project_for_tenant(db, project_id, context)
        return project_response(apply_project_update(db, project_id, req, tenant_id=context.tenant_id))

    @created_app.post("/projects/{project_id}/media", status_code=201)
    def upload_project_media(
        project_id: str, expectedRevision: int = Form(..., ge=0), file: UploadFile = File(...),
        context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        project = project_for_tenant(db, project_id, context)
        if project.revision != expectedRevision:
            raise HTTPException(status_code=409, detail={"code": "CONCURRENCY_CONFLICT", "message": f"Revision conflict: expected {expectedRevision}"})
        file.file.seek(0, os.SEEK_END)
        upload_size = file.file.tell()
        file.file.seek(0)
        if upload_size <= 0:
            raise HTTPException(status_code=422, detail={"code": "EMPTY_UPLOAD", "message": "Uploaded media is empty"})
        if upload_size > max_upload_bytes:
            raise HTTPException(status_code=413, detail={"code": "UPLOAD_TOO_LARGE", "message": "Media exceeds the configured upload limit"})
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == context.tenant_id, DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes)
            .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=utc_now())
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "Tenant storage quota reached"})
        db.commit()
        try:
            uploaded = created_app.state.video_use.upload_media(project_id, file.filename or "media.mp4", file.content_type, file.file)
            metadata = uploaded.get("metadata") or {}
            duration_seconds = float(metadata.get("durationSeconds") or 0)
            media_type = "video" if metadata.get("video") else "audio"
            media_id = uploaded["mediaId"]
            material = {
                "id": media_id, "name": uploaded.get("fileName") or file.filename or media_id,
                "url": f"/api/video-use/media/{project_id}/{media_id}", "type": media_type,
                "duration": {"value": max(1, round(duration_seconds * 24_000)), "timescale": 24_000},
                "sizeBytes": int(metadata.get("sizeBytes") or upload_size),
            }
            updated = apply_project_update(
                db, project_id,
                UpdateProjectRequest(materials=[*project.materials, material], expectedRevision=expectedRevision),
                tenant_id=context.tenant_id, allow_materials=True,
            )
        except Exception as exc:
            db.execute(update(DBTenant).where(DBTenant.id == context.tenant_id).values(used_storage_bytes=func.max(0, DBTenant.used_storage_bytes - upload_size)))
            db.commit()
            if isinstance(exc, VideoUseError):
                raise HTTPException(status_code=502, detail={"code": "VIDEO_USE_UPLOAD_FAILED", "message": str(exc)}) from exc
            raise
        finally:
            file.file.close()
        return {"material": material, "project": project_response(updated)}

    def proxy_video_use_stream(path: str):
        def body():
            with created_app.state.video_use.stream(path) as response:
                yield from response.iter_bytes()
        return StreamingResponse(body(), media_type="application/octet-stream")

    @created_app.get("/video-use/media/{project_id}/{media_id}")
    def stream_project_media(project_id: str, media_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        project = project_for_tenant(db, project_id, context)
        if not any(material.get("id") == media_id for material in project.materials):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND", "message": "Media not found"})
        return proxy_video_use_stream(f"/media/{project_id}/{media_id}")

    @created_app.post("/projects/{project_id}/render", status_code=202)
    def start_render_task(project_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        require_roles(context, "owner", "editor")
        begin_serialized_write(db)
        project = project_for_tenant(db, project_id, context)
        payload, reserved_seconds = build_render_payload(project)
        tenant_query = select(DBTenant).where(DBTenant.id == context.tenant_id)
        if app_engine.dialect.name != "sqlite":
            tenant_query = tenant_query.with_for_update()
        tenant = db.execute(tenant_query).scalar_one()
        refresh_quota_period(db, tenant)
        active = db.execute(select(func.count(DBRenderTask.id)).where(DBRenderTask.tenant_id == context.tenant_id, DBRenderTask.status.in_(ACTIVE_TASK_STATES))).scalar_one()
        if active >= tenant.concurrent_render_quota:
            raise HTTPException(status_code=429, detail={"code": "RENDER_CONCURRENCY_QUOTA_EXCEEDED", "message": "Concurrent render quota reached"})
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == context.tenant_id, DBTenant.render_seconds_used + reserved_seconds <= DBTenant.monthly_render_seconds_quota)
            .values(render_seconds_used=DBTenant.render_seconds_used + reserved_seconds, updated_at=utc_now())
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "RENDER_SECONDS_QUOTA_EXCEEDED", "message": "Monthly render-seconds quota reached"})
        now = utc_now()
        task_id = str(uuid.uuid4())
        payload["requestId"] = task_id
        task = DBRenderTask(
            id=task_id, tenant_id=context.tenant_id, project_id=project_id,
            requested_by=context.user_id, status="queued", progress=0,
            message="Queued for a render worker", engine="video-use", render_payload=payload,
            attempts=0, max_attempts=3, reserved_seconds=reserved_seconds,
            created_at=now, updated_at=now,
        )
        db.add(task)
        db.commit()
        return {**task_response(task), "mock": False}

    @created_app.get("/render-tasks")
    def list_render_tasks(projectId: str | None = Query(default=None), context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        query = select(DBRenderTask).where(DBRenderTask.tenant_id == context.tenant_id)
        if projectId:
            project_for_tenant(db, projectId, context)
            query = query.where(DBRenderTask.project_id == projectId)
        tasks = db.execute(query.order_by(DBRenderTask.created_at.desc()).limit(100)).scalars()
        return [task_response(task) for task in tasks]

    @created_app.get("/renders/{task_id}/artifact")
    def stream_render_artifact(task_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        task = db.execute(select(DBRenderTask).where(DBRenderTask.id == task_id, DBRenderTask.tenant_id == context.tenant_id)).scalar_one_or_none()
        if task is None or task.status != "completed" or not task.upstream_job_id:
            raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND", "message": "Render artifact not found"})
        return proxy_video_use_stream(f"/jobs/{task.upstream_job_id}/artifact")

    @created_app.post("/internal/render-tasks/recover")
    def recover_tasks(
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        now = utc_now()
        stale = list(db.execute(select(DBRenderTask).where(DBRenderTask.status.in_({"dispatching", "processing"}), DBRenderTask.lease_expires_at < now)).scalars())
        recovered = []
        for task in stale:
            task.status = "processing" if task.upstream_job_id else "queued"
            task.lease_owner = None
            task.lease_expires_at = None
            task.message = "Recovered after worker lease expired"
            task.updated_at = now
            recovered.append(task.id)
        db.commit()
        return {"workerId": x_worker_id, "recoveredTaskIds": recovered}

    @created_app.post("/internal/render-tasks/claim")
    def claim_task(
        response: Response,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        now = utc_now()
        begin_serialized_write(db)
        claim_query = (
            select(DBRenderTask)
            .where(
                DBRenderTask.status.in_({"queued", "dispatching", "processing"}),
                or_(DBRenderTask.lease_expires_at.is_(None), DBRenderTask.lease_expires_at < now),
            )
            .order_by(DBRenderTask.created_at.asc())
            .limit(1)
        )
        if app_engine.dialect.name != "sqlite":
            claim_query = claim_query.with_for_update(skip_locked=True)
        task = db.execute(claim_query).scalar_one_or_none()
        if task is None:
            response.status_code = 204
            return response
        if task.status in {"queued", "dispatching"} and not task.upstream_job_id:
            task.status = "dispatching"
            task.attempts += 1
        else:
            task.status = "processing"
        task.lease_owner = x_worker_id
        task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        task.updated_at = now
        db.commit()
        return {
            **task_response(task),
            "renderPayload": task.render_payload,
            "upstreamJobId": task.upstream_job_id,
            "leaseSeconds": lease_seconds,
        }

    @created_app.post("/internal/render-tasks/{task_id}/update")
    def update_task_from_worker(
        task_id: str, req: WorkerTaskUpdateRequest,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        task = db.execute(select(DBRenderTask).where(DBRenderTask.id == task_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "Task not found"})
        if task.lease_owner != x_worker_id:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "Worker no longer owns this task lease"})
        now = utc_now()
        if req.upstreamJobId:
            task.upstream_job_id = req.upstreamJobId
        task.progress = req.progress
        task.message = req.message
        task.error = req.error
        if req.status == "failed" and req.retryable and task.attempts < task.max_attempts:
            task.status = "queued"
            task.upstream_job_id = None
            task.lease_owner = None
            task.lease_expires_at = None
        else:
            task.status = req.status
            if req.status in {"completed", "failed"}:
                task.completed_at = now
                task.lease_owner = None
                task.lease_expires_at = None
            else:
                task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        task.updated_at = now
        db.commit()
        return task_response(task)

    @created_app.get("/events")
    async def sse_events(
        once: bool = Query(False),
        context: AuthContext = Depends(context_dependency),
    ):
        async def event_generator():
            while True:
                heartbeat = {"timestamp": iso_utc(utc_now())}
                yield f"event: heartbeat\ndata: {json.dumps(heartbeat)}\n\n"
                with internal_sessions() as event_db:
                    tasks = event_db.execute(
                        select(DBRenderTask)
                        .where(DBRenderTask.tenant_id == context.tenant_id)
                        .order_by(DBRenderTask.created_at.desc())
                        .limit(100)
                    ).scalars()
                    for task in tasks:
                        yield "event: task_progress\n" f"data: {json.dumps(task_response(task), separators=(',', ':'))}\n\n"
                if once:
                    break
                await asyncio.sleep(1)
        return StreamingResponse(event_generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return created_app


app = create_app()
