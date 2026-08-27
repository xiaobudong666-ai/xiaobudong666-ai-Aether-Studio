from __future__ import annotations

import asyncio
import datetime
import hashlib
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
from sqlalchemy.exc import IntegrityError
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
from .models import (
    DBAdoption,
    DBAssetVersion,
    DBCandidate,
    DBExternalTask,
    DBGenerationAttempt,
    DBGenerationEvent,
    DBGenerationTask,
    DBMasterRevision,
    DBProject,
    DBRenderTask,
    DBRightsSnapshot,
    DBSession,
    DBTenant,
    DBUser,
)
from .moneyprinter_adapter import MoneyPrinterTurboAdapter
from .generation_tasks import (
    ACTIVE_GENERATION_STATES,
    RETRYABLE_GENERATION_STATES,
    TERMINAL_GENERATION_STATES,
    WORKER_TRANSITIONS,
    add_event as add_generation_event,
    build_capability_snapshot,
    capability_snapshot_valid,
    current_attempt as current_generation_attempt,
    request_hash as generation_request_hash,
    safe_error_message,
    task_response as generation_task_response,
)
from .schemas import (
    AdoptCandidateRequest,
    CreateProjectRequest,
    CreateRightsSnapshotRequest,
    CreateUserRequest,
    GenerationTaskRequest,
    GenerationWorkerHeartbeatRequest,
    GenerationWorkerTransitionRequest,
    LoginRequest,
    MoneyPrinterGenerateRequest,
    ProjectResponse,
    UpdateProjectRequest,
    WorkerTaskUpdateRequest,
)
from .task_status import (
    canonical_task_status,
    database_status_values,
    legacy_task_status,
)
from .timeline_render import build_render_payload
from .video_use_adapter import VideoUseAdapter, VideoUseError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.main")

DatabaseDependency = Callable[[], Generator[Session, None, None]]
ACTIVE_TASK_STATES = database_status_values("QUEUED", "RUNNING")


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
    canonical_status = canonical_task_status(task.status)
    payload = {
        "taskId": task.id,
        "projectId": task.project_id,
        "progress": task.progress,
        "status": legacy_task_status(canonical_status),
        "canonicalStatus": canonical_status,
        "message": task.message,
        "engine": task.engine,
        "attempts": task.attempts,
        "createdAt": iso_utc(task.created_at),
        "updatedAt": iso_utc(task.updated_at),
    }
    if canonical_status == "SUCCEEDED":
        payload["artifactUrl"] = f"/api/renders/{task.id}/artifact"
    if task.error:
        payload["error"] = task.error
    return payload


def asset_version_response(asset: DBAssetVersion) -> dict:
    return {
        "id": asset.id,
        "projectId": asset.project_id,
        "mediaId": asset.media_id,
        "versionNo": asset.version_no,
        "sha256": asset.sha256,
        "mediaType": asset.media_type,
        "contentType": asset.content_type,
        "sizeBytes": asset.size_bytes,
        "probe": asset.probe_json,
        "createdBy": asset.created_by,
        "createdAt": iso_utc(asset.created_at),
    }


def rights_snapshot_response(snapshot: DBRightsSnapshot) -> dict:
    return {
        "id": snapshot.id,
        "assetVersionId": snapshot.asset_version_id,
        "status": snapshot.status,
        "purpose": snapshot.purpose,
        "territory": snapshot.territory,
        "validFrom": iso_utc(snapshot.valid_from) if snapshot.valid_from else None,
        "validUntil": iso_utc(snapshot.valid_until) if snapshot.valid_until else None,
        "evidenceRef": snapshot.evidence_ref,
        "capturedBy": snapshot.captured_by,
        "capturedAt": iso_utc(snapshot.captured_at),
    }


def _as_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def rights_decision(snapshot: DBRightsSnapshot | None, now: datetime.datetime) -> dict:
    if snapshot is None:
        return {"allowed": False, "code": "RIGHTS_MISSING"}
    if snapshot.status != "ALLOWED":
        return {"allowed": False, "code": f"RIGHTS_{snapshot.status}"}
    valid_from = _as_utc(snapshot.valid_from)
    valid_until = _as_utc(snapshot.valid_until)
    if valid_from is not None and now < valid_from:
        return {"allowed": False, "code": "RIGHTS_NOT_YET_VALID"}
    if valid_until is not None and now >= valid_until:
        return {"allowed": False, "code": "RIGHTS_EXPIRED"}
    return {"allowed": True, "code": "RIGHTS_ALLOWED"}


def candidate_response(candidate: DBCandidate) -> dict:
    return {
        "id": candidate.id,
        "projectId": candidate.project_id,
        "taskId": candidate.task_id,
        "artifactRef": candidate.artifact_ref,
        "inputRevision": candidate.input_revision,
        "status": candidate.status,
        "createdAt": iso_utc(candidate.created_at),
    }


def master_response(master: DBMasterRevision, adoption: DBAdoption) -> dict:
    return {
        "id": master.id,
        "projectId": master.project_id,
        "revisionNo": master.revision_no,
        "artifactRef": master.artifact_ref,
        "sha256": master.sha256,
        "createdAt": iso_utc(master.created_at),
        "adoption": {
            "id": adoption.id,
            "candidateId": adoption.candidate_id,
            "adoptedBy": adoption.adopted_by,
            "adoptedAt": iso_utc(adoption.adopted_at),
            "reason": adoption.reason,
            "supersedesId": adoption.supersedes_id,
        },
    }


def apply_project_update(
    db: Session,
    project_id: str,
    req: UpdateProjectRequest,
    tenant_id: str | None = None,
    allow_materials: bool = False,
    commit: bool = True,
) -> DBProject:
    if req.materials is not None and not allow_materials:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MATERIALS_SERVER_MANAGED",
                "message": "项目素材只能通过媒体上传接口修改",
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
                detail={"code": "PROJECT_NOT_FOUND", "message": "未找到该项目"},
            )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "CONCURRENCY_CONFLICT",
                "message": "项目版本冲突，服务器中的项目已经发生变化",
            },
        )
    if commit:
        db.commit()
    else:
        db.flush()
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
    generation_provider_mode: str | None = None,
) -> FastAPI:
    del render_step_delay  # retained for backwards-compatible test construction
    internal_sessions = sessionmaker(autocommit=False, autoflush=False, bind=app_engine)
    resolved_worker_token = worker_token if worker_token is not None else os.environ.get("AETHER_WORKER_TOKEN", "")
    resolved_cookie_secure = cookie_secure if cookie_secure is not None else os.environ.get("AETHER_COOKIE_SECURE", "true").lower() == "true"
    resolved_enforce_csrf = enforce_csrf if enforce_csrf is not None else os.environ.get("AETHER_ENFORCE_CSRF", "true").lower() == "true"
    session_hours = max(1, int(os.environ.get("AETHER_SESSION_HOURS", "12")))
    lease_seconds = max(15, int(os.environ.get("AETHER_TASK_LEASE_SECONDS", "60")))
    max_upload_bytes = min(
        2 * 1024**3,
        int(os.environ.get("AETHER_MAX_UPLOAD_BYTES", str(2 * 1024**3))),
    )
    resolved_generation_provider_mode = (
        generation_provider_mode
        if generation_provider_mode is not None
        else os.environ.get("AETHER_GENERATION_PROVIDER_MODE", "disabled")
    )
    if resolved_generation_provider_mode not in {"disabled", "deterministic-fake"}:
        resolved_generation_provider_mode = "disabled"

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

    created_app = FastAPI(title="Aether Studio 接口服务", version="1.1.0", lifespan=lifespan)
    created_app.state.moneyprinter = MoneyPrinterTurboAdapter()
    created_app.state.video_use = video_use_adapter or VideoUseAdapter()
    created_app.state.generation_provider_mode = resolved_generation_provider_mode
    created_app.state.generation_capability_snapshot = build_capability_snapshot(
        resolved_generation_provider_mode
    )
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
                content=json.dumps({"detail": {"code": "CSRF_REQUIRED", "message": "缺少同源请求校验信息"}}, ensure_ascii=False),
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
                detail={"code": "PROJECT_NOT_FOUND", "message": "未找到该项目"},
            )
        return project

    def asset_for_tenant(
        db: Session,
        project_id: str,
        asset_version_id: str,
        context: AuthContext,
    ) -> DBAssetVersion:
        asset = db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.id == asset_version_id,
                DBAssetVersion.project_id == project_id,
                DBAssetVersion.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if asset is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "ASSET_VERSION_NOT_FOUND", "message": "未找到该资产版本"},
            )
        return asset

    def latest_rights_snapshot(
        db: Session,
        asset_version_id: str,
        purpose: str | None = None,
    ) -> DBRightsSnapshot | None:
        query = select(DBRightsSnapshot).where(
            DBRightsSnapshot.asset_version_id == asset_version_id,
        )
        if purpose:
            query = query.where(DBRightsSnapshot.purpose == purpose)
        return db.execute(
            query.order_by(DBRightsSnapshot.captured_at.desc()).limit(1)
        ).scalar_one_or_none()

    def generation_rights(db: Session, task: DBGenerationTask) -> dict:
        if not task.asset_version_id:
            return {"allowed": False, "code": "RIGHTS_MISSING"}
        return rights_decision(
            latest_rights_snapshot(db, task.asset_version_id, "EXPORT"),
            utc_now(),
        )

    def generation_task_for_tenant(
        db: Session,
        project_id: str,
        task_id: str,
        context: AuthContext,
    ) -> DBGenerationTask:
        task = db.execute(
            select(DBGenerationTask).where(
                DBGenerationTask.id == task_id,
                DBGenerationTask.project_id == project_id,
                DBGenerationTask.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if task is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"},
            )
        return task

    def current_generation_capabilities(*, refresh: bool = False) -> dict:
        snapshot = created_app.state.generation_capability_snapshot
        expires_at = datetime.datetime.fromisoformat(
            str(snapshot["expiresAt"]).replace("Z", "+00:00")
        )
        if refresh or utc_now() >= expires_at:
            snapshot = build_capability_snapshot(created_app.state.generation_provider_mode)
            created_app.state.generation_capability_snapshot = snapshot
        return snapshot

    def validate_generation_request(
        db: Session,
        project: DBProject,
        req: GenerationTaskRequest,
        context: AuthContext,
    ) -> dict:
        if project.revision != req.expectedProjectRevision:
            raise HTTPException(
                status_code=409,
                detail={"code": "PROJECT_REVISION_CONFLICT", "message": "项目版本已变化，请重新预检"},
            )
        snapshot = current_generation_capabilities()
        valid, code = capability_snapshot_valid(
            snapshot, req.capabilitySnapshotHash, utc_now()
        )
        if not valid:
            status_code = 503 if code in {"PROVIDER_DISABLED", "PROVIDER_UNHEALTHY"} else 409
            raise HTTPException(
                status_code=status_code,
                detail={"code": code, "message": "生成服务能力快照不可用，请重新预检"},
            )
        if req.voiceName not in snapshot["voices"]:
            raise HTTPException(
                status_code=422,
                detail={"code": "VOICE_UNSUPPORTED", "message": "所选声音不在当前能力范围内"},
            )
        if req.outputCount > int(snapshot["maxOutputs"]):
            raise HTTPException(
                status_code=422,
                detail={"code": "OUTPUT_COUNT_UNSUPPORTED", "message": "输出数量超过当前 Provider 能力"},
            )
        if req.inputAssetVersionIds:
            count = db.execute(
                select(func.count(DBAssetVersion.id)).where(
                    DBAssetVersion.id.in_(req.inputAssetVersionIds),
                    DBAssetVersion.project_id == project.id,
                    DBAssetVersion.tenant_id == context.tenant_id,
                )
            ).scalar_one()
            if count != len(req.inputAssetVersionIds):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "INPUT_ASSET_SCOPE_INVALID", "message": "参考素材版本不存在或不属于当前项目"},
                )
        return snapshot

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
            raise HTTPException(status_code=401, detail={"code": "WORKER_AUTH_FAILED", "message": "工作节点认证失败"})

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
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "邮箱或密码不正确"})
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
            raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "该邮箱已经存在"})
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
            logger.exception("MoneyPrinterTurbo task submission failed")
            raise HTTPException(status_code=502, detail={"code": "MONEYPRINTER_API_ERROR", "message": "MoneyPrinterTurbo 任务提交失败"}) from exc
        now = utc_now()
        db.add(DBExternalTask(id=task_id, tenant_id=context.tenant_id, requested_by=context.user_id, engine="moneyprinter", status="submitted", created_at=now, updated_at=now))
        db.commit()
        return {"task_id": task_id, "status": "submitted"}

    @created_app.get("/moneyprinter/status/{task_id}")
    def moneyprinter_status(task_id: str, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        task = db.execute(select(DBExternalTask).where(DBExternalTask.id == task_id, DBExternalTask.tenant_id == context.tenant_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "未找到该任务"})
        try:
            payload = created_app.state.moneyprinter.get_task_status(task_id)
        except Exception as exc:
            logger.exception("MoneyPrinterTurbo status query failed")
            raise HTTPException(status_code=502, detail={"code": "MONEYPRINTER_STATUS_ERROR", "message": "MoneyPrinterTurbo 任务状态查询失败"}) from exc
        task.status = str(payload.get("status", task.status))
        task.updated_at = utc_now()
        db.commit()
        return payload

    @created_app.get("/generation/providers/moneyprinter/capabilities")
    def generation_capabilities(
        _context: AuthContext = Depends(context_dependency),
    ):
        return current_generation_capabilities()

    @created_app.post("/projects/{project_id}/generation-tasks/validate")
    def validate_generation_task(
        project_id: str,
        req: GenerationTaskRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        project = project_for_tenant(db, project_id, context)
        snapshot = validate_generation_request(db, project, req, context)
        return {
            "allowed": True,
            "status": "PREFLIGHT",
            "capabilitySnapshotHash": snapshot["snapshotHash"],
            "projectRevision": project.revision,
        }

    @created_app.post("/projects/{project_id}/generation-tasks", status_code=202)
    def create_generation_task(
        project_id: str,
        req: GenerationTaskRequest,
        idempotency_key_header: str | None = Header(default=None, alias="Idempotency-Key", min_length=8, max_length=128),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        begin_serialized_write(db)
        project = project_for_tenant(db, project_id, context)
        idempotency_key = str(req.idempotencyKey)
        if idempotency_key_header is not None and idempotency_key_header != idempotency_key:
            raise HTTPException(
                status_code=409,
                detail={"code": "IDEMPOTENCY_KEY_MISMATCH", "message": "请求体与请求头的幂等键不一致"},
            )
        payload = req.model_dump(mode="json")
        payload_hash = generation_request_hash(payload)
        existing = db.execute(
            select(DBGenerationTask).where(
                DBGenerationTask.tenant_id == context.tenant_id,
                DBGenerationTask.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.project_id != project_id or existing.request_hash != payload_hash:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "该幂等键已用于不同生成请求"},
                )
            return generation_task_response(
                db, existing, rights=generation_rights(db, existing), include_history=True
            )
        snapshot = validate_generation_request(db, project, req, context)
        now = utc_now()
        task = DBGenerationTask(
            id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            project_id=project_id,
            requested_by=context.user_id,
            provider="moneyprinter",
            status="QUEUED",
            progress=0,
            message="生成任务已进入受治理队列",
            request_json=payload,
            request_hash=payload_hash,
            capability_snapshot_json=snapshot,
            capability_snapshot_hash=snapshot["snapshotHash"],
            idempotency_key=idempotency_key,
            attempts=1,
            max_attempts=3,
            created_at=now,
            updated_at=now,
        )
        attempt = DBGenerationAttempt(
            id=str(uuid.uuid4()),
            generation_task_id=task.id,
            attempt_no=1,
            status="QUEUED",
            reconciliation_state="NOT_SUBMITTED",
            created_at=now,
        )
        db.add_all([task, attempt])
        add_generation_event(
            db, task, attempt=attempt, event_type="TASK_CREATED",
            actor_type="USER", actor_id=context.user_id, to_status="QUEUED", now=now,
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            concurrent = db.execute(
                select(DBGenerationTask).where(
                    DBGenerationTask.tenant_id == context.tenant_id,
                    DBGenerationTask.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if concurrent is not None and concurrent.project_id == project_id and concurrent.request_hash == payload_hash:
                return generation_task_response(
                    db, concurrent, rights=generation_rights(db, concurrent), include_history=True
                )
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_CREATE_CONFLICT", "message": "生成任务发生并发冲突"},
            ) from exc
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )

    @created_app.get("/projects/{project_id}/generation-tasks")
    def list_generation_tasks(
        project_id: str,
        cursor: int = Query(default=0, ge=0),
        pageSize: int = Query(default=20, ge=1, le=100),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        rows = list(db.execute(
            select(DBGenerationTask)
            .where(
                DBGenerationTask.project_id == project_id,
                DBGenerationTask.tenant_id == context.tenant_id,
            )
            .order_by(DBGenerationTask.created_at.desc(), DBGenerationTask.id.desc())
            .offset(cursor)
            .limit(pageSize + 1)
        ).scalars())
        has_more = len(rows) > pageSize
        items = rows[:pageSize]
        return {
            "items": [
                generation_task_response(db, task, rights=generation_rights(db, task))
                for task in items
            ],
            "nextCursor": cursor + pageSize if has_more else None,
        }

    @created_app.get("/projects/{project_id}/generation-tasks/{task_id}")
    def get_generation_task(
        project_id: str,
        task_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        task = generation_task_for_tenant(db, project_id, task_id, context)
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )

    @created_app.post("/projects/{project_id}/generation-tasks/{task_id}/cancel")
    def cancel_generation_task(
        project_id: str,
        task_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        task = generation_task_for_tenant(db, project_id, task_id, context)
        if task.cancel_requested_at is None and task.status not in TERMINAL_GENERATION_STATES:
            now = utc_now()
            previous = task.status
            attempt = current_generation_attempt(db, task)
            task.cancel_requested_at = now
            task.status = "CANCELED"
            task.message = "生成任务已取消"
            task.completed_at = now
            task.updated_at = now
            task.lease_owner = None
            task.lease_expires_at = None
            attempt.status = "CANCELED"
            attempt.completed_at = now
            add_generation_event(
                db, task, attempt=attempt, event_type="TASK_CANCELED",
                actor_type="USER", actor_id=context.user_id,
                from_status=previous, to_status="CANCELED", now=now,
            )
            db.commit()
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )

    @created_app.post("/projects/{project_id}/generation-tasks/{task_id}/retry", status_code=202)
    def retry_generation_task(
        project_id: str,
        task_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        task = generation_task_for_tenant(db, project_id, task_id, context)
        if task.status not in RETRYABLE_GENERATION_STATES or (task.error_code or "").startswith("NON_RETRYABLE_"):
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_NOT_RETRYABLE", "message": "当前任务状态不可重试"},
            )
        if task.attempts >= task.max_attempts:
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_RETRY_EXHAUSTED", "message": "生成任务已达到最大尝试次数"},
            )
        now = utc_now()
        previous = task.status
        task.attempts += 1
        task.status = "QUEUED"
        task.progress = 0
        task.message = "生成任务已排队重试"
        task.upstream_job_id = None
        task.provider_artifact_id = None
        task.error_code = None
        task.error_message = None
        task.completed_at = None
        task.lease_owner = None
        task.lease_expires_at = None
        task.updated_at = now
        attempt = DBGenerationAttempt(
            id=str(uuid.uuid4()), generation_task_id=task.id,
            attempt_no=task.attempts, status="QUEUED",
            reconciliation_state="NOT_SUBMITTED", created_at=now,
        )
        db.add(attempt)
        add_generation_event(
            db, task, attempt=attempt, event_type="TASK_RETRIED",
            actor_type="USER", actor_id=context.user_id,
            from_status=previous, to_status="QUEUED",
            metadata={"attempt": task.attempts}, now=now,
        )
        db.commit()
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )

    @created_app.post("/internal/generation-tasks/claim")
    def claim_generation_task(
        response: Response,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        now = utc_now()
        begin_serialized_write(db)
        claim_query = (
            select(DBGenerationTask)
            .where(
                DBGenerationTask.status.in_(ACTIVE_GENERATION_STATES),
                or_(DBGenerationTask.lease_expires_at.is_(None), DBGenerationTask.lease_expires_at < now),
                DBGenerationTask.cancel_requested_at.is_(None),
            )
            .order_by(DBGenerationTask.created_at.asc(), DBGenerationTask.id.asc())
            .limit(1)
        )
        if app_engine.dialect.name != "sqlite":
            claim_query = claim_query.with_for_update(skip_locked=True)
        task = db.execute(claim_query).scalar_one_or_none()
        if task is None:
            db.rollback()
            response.status_code = 204
            return response
        previous = task.status
        attempt = current_generation_attempt(db, task)
        if task.status == "QUEUED" or not task.upstream_job_id:
            task.status = "SUBMITTING"
            task.message = "工作节点正在提交生成任务"
            attempt.status = "SUBMITTING"
            attempt.submission_started_at = attempt.submission_started_at or now
        else:
            task.message = "工作节点正在恢复生成任务"
        task.started_at = task.started_at or now
        task.lease_owner = x_worker_id
        task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        task.updated_at = now
        add_generation_event(
            db, task, attempt=attempt,
            event_type="LEASE_RECOVERED" if previous != "QUEUED" else "TASK_CLAIMED",
            actor_type="WORKER", actor_id=x_worker_id,
            from_status=previous, to_status=task.status,
            metadata={"leaseSeconds": lease_seconds}, now=now,
        )
        db.commit()
        return {
            "taskId": task.id,
            "projectId": task.project_id,
            "attempt": task.attempts,
            "status": task.status,
            "request": task.request_json,
            "upstreamJobId": task.upstream_job_id,
            "providerArtifactId": task.provider_artifact_id,
            "providerMode": task.capability_snapshot_json.get("mode"),
            "leaseSeconds": lease_seconds,
        }

    @created_app.post("/internal/generation-tasks/{task_id}/heartbeat")
    def heartbeat_generation_task(
        task_id: str,
        _req: GenerationWorkerHeartbeatRequest,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        task = db.execute(select(DBGenerationTask).where(DBGenerationTask.id == task_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if task.status not in ACTIVE_GENERATION_STATES or task.cancel_requested_at is not None:
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_ACTIVE", "message": "任务已不再处于可续租状态"})
        task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        task.updated_at = now
        db.commit()
        return {"taskId": task.id, "leaseExpiresAt": iso_utc(task.lease_expires_at)}

    @created_app.post("/internal/generation-tasks/{task_id}/transition")
    def transition_generation_task(
        task_id: str,
        req: GenerationWorkerTransitionRequest,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        task = db.execute(select(DBGenerationTask).where(DBGenerationTask.id == task_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if task.cancel_requested_at is not None or task.status == "CANCELED":
            raise HTTPException(status_code=409, detail={"code": "TASK_CANCELED", "message": "任务已取消，迟到响应已隔离"})
        if req.status != task.status and req.status not in WORKER_TRANSITIONS.get(task.status, set()):
            raise HTTPException(status_code=409, detail={"code": "ILLEGAL_GENERATION_TRANSITION", "message": "生成任务状态迁移不合法"})
        if req.upstreamJobId and task.upstream_job_id and req.upstreamJobId != task.upstream_job_id:
            raise HTTPException(status_code=409, detail={"code": "UPSTREAM_ID_IMMUTABLE", "message": "上游任务编号不可替换"})
        previous = task.status
        attempt = current_generation_attempt(db, task)
        if req.upstreamJobId:
            task.upstream_job_id = req.upstreamJobId
            attempt.upstream_job_id = attempt.upstream_job_id or req.upstreamJobId
            attempt.reconciliation_state = "SUBMITTED"
        if req.providerArtifactId:
            task.provider_artifact_id = req.providerArtifactId
        task.status = req.status
        task.progress = req.progress
        task.message = req.message
        task.error_code = req.errorCode
        task.error_message = safe_error_message(req.errorMessage)
        task.updated_at = now
        attempt.status = req.status
        attempt.error_code = req.errorCode
        attempt.error_message = safe_error_message(req.errorMessage)
        if req.status == "UNKNOWN":
            attempt.reconciliation_state = "UNKNOWN"
        if req.status in {"FAILED", "CANCELED", "UNKNOWN", "PARTIAL"}:
            if req.status == "FAILED" and not req.retryable and task.error_code:
                task.error_code = f"NON_RETRYABLE_{task.error_code}"
                attempt.error_code = task.error_code
            task.completed_at = now
            attempt.completed_at = now
            task.lease_owner = None
            task.lease_expires_at = None
        else:
            task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        add_generation_event(
            db, task, attempt=attempt, event_type="WORKER_TRANSITION",
            actor_type="WORKER", actor_id=x_worker_id,
            from_status=previous, to_status=req.status,
            metadata={"progress": req.progress}, now=now,
        )
        db.commit()
        return {"taskId": task.id, "status": task.status, "progress": task.progress}

    @created_app.post("/internal/generation-tasks/{task_id}/artifact-intake", status_code=201)
    def intake_generation_artifact(
        task_id: str,
        providerArtifactId: str = Form(..., min_length=1, max_length=256),
        file: UploadFile = File(...),
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        if any(marker in providerArtifactId for marker in ("://", "..", "\\", "/")):
            raise HTTPException(status_code=422, detail={"code": "ARTIFACT_ID_INVALID", "message": "产物编号格式不安全"})
        task = db.execute(select(DBGenerationTask).where(DBGenerationTask.id == task_id)).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        if task.asset_version_id:
            completion = db.execute(
                select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == task.id,
                    DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
                    DBGenerationEvent.actor_id == x_worker_id,
                )
            ).scalar_one_or_none()
            if task.provider_artifact_id == providerArtifactId and completion is not None:
                return generation_task_response(db, task, rights=generation_rights(db, task), include_history=True)
            raise HTTPException(status_code=409, detail={"code": "ARTIFACT_ALREADY_INGESTED", "message": "任务已绑定其他受治理产物"})
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if task.cancel_requested_at is not None or task.status == "CANCELED":
            raise HTTPException(status_code=409, detail={"code": "TASK_CANCELED", "message": "任务已取消，迟到产物已隔离"})
        if task.status not in {"RUNNING", "INGESTING"}:
            raise HTTPException(status_code=409, detail={"code": "ARTIFACT_INTAKE_NOT_ALLOWED", "message": "当前状态不允许接收产物"})
        if file.content_type != "video/mp4":
            raise HTTPException(status_code=415, detail={"code": "ARTIFACT_CONTENT_TYPE_INVALID", "message": "仅接受受信任的 MP4 字节流"})
        file.file.seek(0, os.SEEK_END)
        upload_size = file.file.tell()
        file.file.seek(0)
        if upload_size <= 0:
            raise HTTPException(status_code=422, detail={"code": "EMPTY_UPLOAD", "message": "生成产物为空"})
        if upload_size > max_upload_bytes:
            raise HTTPException(status_code=413, detail={"code": "UPLOAD_TOO_LARGE", "message": "生成产物超过系统大小限制"})
        digest = hashlib.sha256()
        while chunk := file.file.read(1024 * 1024):
            digest.update(chunk)
        file.file.seek(0)
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == task.tenant_id, DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes)
            .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=now)
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "团队存储空间已达到配额上限"})
        db.commit()
        try:
            uploaded = created_app.state.video_use.upload_media(
                task.project_id, f"generation-{task.id}.mp4", "video/mp4", file.file
            )
            metadata = uploaded.get("metadata") or {}
            if not metadata.get("video") or float(metadata.get("durationSeconds") or 0) <= 0:
                raise ValueError("generated artifact did not pass media probe")
            media_id = str(uploaded.get("mediaId") or "")
            if not media_id or uploaded.get("projectId") != task.project_id:
                raise ValueError("generated artifact returned invalid media scope")
            project = db.execute(select(DBProject).where(DBProject.id == task.project_id, DBProject.tenant_id == task.tenant_id)).scalar_one()
            provenance = {
                "provider": task.provider,
                "providerArtifactId": providerArtifactId,
                "generationTaskId": task.id,
                "attempt": task.attempts,
                "capabilitySnapshotHash": task.capability_snapshot_hash,
            }
            probe = {**metadata, "provenance": provenance}
            duration_seconds = float(metadata["durationSeconds"])
            size_bytes = int(metadata.get("sizeBytes") or upload_size)
            material = {
                "id": media_id,
                "name": uploaded.get("fileName") or f"generation-{task.id}.mp4",
                "url": f"/api/video-use/media/{task.project_id}/{media_id}",
                "type": "video",
                "contentType": "video/mp4",
                "duration": {"value": max(1, round(duration_seconds * 24_000)), "timescale": 24_000},
                "sizeBytes": size_bytes,
            }
            if not any(item.get("id") == media_id for item in project.materials):
                project.materials = [*project.materials, material]
                project.revision += 1
                project.updated_at = now
            next_version = (db.execute(
                select(func.max(DBAssetVersion.version_no)).where(
                    DBAssetVersion.project_id == task.project_id,
                    DBAssetVersion.media_id == media_id,
                )
            ).scalar_one_or_none() or 0) + 1
            asset = DBAssetVersion(
                id=str(uuid.uuid4()), tenant_id=task.tenant_id, project_id=task.project_id,
                media_id=media_id, version_no=next_version, sha256=digest.hexdigest(),
                media_type="video", content_type="video/mp4", size_bytes=size_bytes,
                probe_json=probe, created_by=task.requested_by, created_at=now,
            )
            previous = task.status
            task.status = "RIGHTS_BLOCKED"
            task.progress = 100
            task.message = "生成产物已入库，等待权利审核"
            task.provider_artifact_id = providerArtifactId
            task.media_id = media_id
            task.asset_version_id = asset.id
            task.completed_at = now
            task.updated_at = now
            task.lease_owner = None
            task.lease_expires_at = None
            attempt = current_generation_attempt(db, task)
            attempt.status = "RIGHTS_BLOCKED"
            attempt.reconciliation_state = "ARTIFACT_INGESTED"
            attempt.completed_at = now
            db.add(asset)
            add_generation_event(
                db, task, attempt=attempt, event_type="ARTIFACT_INGESTED",
                actor_type="WORKER", actor_id=x_worker_id,
                from_status=previous, to_status="RIGHTS_BLOCKED",
                metadata={"assetVersionId": asset.id, "sha256": asset.sha256, "sizeBytes": size_bytes}, now=now,
            )
            db.commit()
        except Exception as exc:
            db.rollback()
            db.execute(update(DBTenant).where(DBTenant.id == task.tenant_id).values(used_storage_bytes=func.max(0, DBTenant.used_storage_bytes - upload_size)))
            failed_task = db.execute(select(DBGenerationTask).where(DBGenerationTask.id == task_id)).scalar_one()
            failed_attempt = current_generation_attempt(db, failed_task)
            failed_task.status = "FAILED"
            failed_task.error_code = "NON_RETRYABLE_ARTIFACT_VALIDATION_FAILED" if isinstance(exc, ValueError) else "ARTIFACT_INGEST_FAILED"
            failed_task.error_message = "生成产物未通过受治理媒体校验" if isinstance(exc, ValueError) else "生成产物入库失败"
            failed_task.completed_at = utc_now()
            failed_task.updated_at = failed_task.completed_at
            failed_task.lease_owner = None
            failed_task.lease_expires_at = None
            failed_attempt.status = "FAILED"
            failed_attempt.error_code = failed_task.error_code
            failed_attempt.error_message = failed_task.error_message
            failed_attempt.completed_at = failed_task.completed_at
            add_generation_event(
                db, failed_task, attempt=failed_attempt, event_type="ARTIFACT_INGEST_FAILED",
                actor_type="SYSTEM", actor_id="api", from_status=task.status,
                to_status="FAILED", metadata={"compensatedQuotaBytes": upload_size}, now=failed_task.completed_at,
            )
            db.commit()
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=422, detail={"code": "ARTIFACT_VALIDATION_FAILED", "message": "生成产物未通过媒体校验"}) from exc
            if isinstance(exc, VideoUseError):
                raise HTTPException(status_code=502, detail={"code": "ARTIFACT_STORE_FAILED", "message": "生成产物存储失败"}) from exc
            raise
        finally:
            file.file.close()
        return generation_task_response(db, task, rights=generation_rights(db, task), include_history=True)

    @created_app.get("/video-use/health")
    def video_use_health():
        return created_app.state.video_use.check_health()

    @created_app.get("/video-use/capabilities")
    def video_use_capabilities():
        try:
            return created_app.state.video_use.get_capabilities()
        except VideoUseError as exc:
            logger.exception("video-use capabilities query failed")
            raise HTTPException(status_code=502, detail={"code": "VIDEO_USE_UNAVAILABLE", "message": "视频处理服务暂时不可用"}) from exc

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
            raise HTTPException(status_code=429, detail={"code": "PROJECT_QUOTA_EXCEEDED", "message": "项目数量已达到配额上限"})
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
            raise HTTPException(status_code=409, detail={"code": "CONCURRENCY_CONFLICT", "message": "项目版本冲突，请重新载入后再操作"})
        file.file.seek(0, os.SEEK_END)
        upload_size = file.file.tell()
        file.file.seek(0)
        if upload_size <= 0:
            raise HTTPException(status_code=422, detail={"code": "EMPTY_UPLOAD", "message": "上传的媒体文件为空"})
        if upload_size > max_upload_bytes:
            raise HTTPException(status_code=413, detail={"code": "UPLOAD_TOO_LARGE", "message": "媒体文件超过系统允许的上传大小"})
        digest = hashlib.sha256()
        while chunk := file.file.read(1024 * 1024):
            digest.update(chunk)
        file.file.seek(0)
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == context.tenant_id, DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes)
            .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=utc_now())
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "团队存储空间已达到配额上限"})
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
                "contentType": file.content_type or ("video/mp4" if media_type == "video" else "audio/mpeg"),
                "duration": {"value": max(1, round(duration_seconds * 24_000)), "timescale": 24_000},
                "sizeBytes": int(metadata.get("sizeBytes") or upload_size),
            }
            updated = apply_project_update(
                db, project_id,
                UpdateProjectRequest(materials=[*project.materials, material], expectedRevision=expectedRevision),
                tenant_id=context.tenant_id, allow_materials=True, commit=False,
            )
            next_version = (
                db.execute(
                    select(func.max(DBAssetVersion.version_no)).where(
                        DBAssetVersion.project_id == project_id,
                        DBAssetVersion.media_id == media_id,
                    )
                ).scalar_one_or_none()
                or 0
            ) + 1
            asset_version = DBAssetVersion(
                id=str(uuid.uuid4()),
                tenant_id=context.tenant_id,
                project_id=project_id,
                media_id=media_id,
                version_no=next_version,
                sha256=digest.hexdigest(),
                media_type=media_type,
                content_type=material["contentType"],
                size_bytes=material["sizeBytes"],
                probe_json=metadata,
                created_by=context.user_id,
                created_at=utc_now(),
            )
            db.add(asset_version)
            db.commit()
            db.refresh(asset_version)
        except Exception as exc:
            db.rollback()
            db.execute(update(DBTenant).where(DBTenant.id == context.tenant_id).values(used_storage_bytes=func.max(0, DBTenant.used_storage_bytes - upload_size)))
            db.commit()
            if isinstance(exc, VideoUseError):
                logger.exception("video-use media upload failed")
                raise HTTPException(status_code=502, detail={"code": "VIDEO_USE_UPLOAD_FAILED", "message": "视频处理服务未能完成媒体上传"}) from exc
            raise
        finally:
            file.file.close()
        return {
            "material": material,
            "assetVersion": asset_version_response(asset_version),
            "project": project_response(updated),
        }

    @created_app.get("/projects/{project_id}/asset-versions")
    def list_asset_versions(
        project_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        assets = db.execute(
            select(DBAssetVersion)
            .where(
                DBAssetVersion.project_id == project_id,
                DBAssetVersion.tenant_id == context.tenant_id,
            )
            .order_by(DBAssetVersion.created_at.asc())
        ).scalars()
        return [asset_version_response(asset) for asset in assets]

    @created_app.post(
        "/projects/{project_id}/asset-versions/{asset_version_id}/rights-snapshots",
        status_code=201,
    )
    def create_rights_snapshot(
        project_id: str,
        asset_version_id: str,
        req: CreateRightsSnapshotRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        project_for_tenant(db, project_id, context)
        asset_for_tenant(db, project_id, asset_version_id, context)
        snapshot = DBRightsSnapshot(
            id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            project_id=project_id,
            asset_version_id=asset_version_id,
            status=req.status,
            purpose=req.purpose.strip(),
            territory=req.territory.strip(),
            valid_from=req.validFrom,
            valid_until=req.validUntil,
            evidence_ref=req.evidenceRef,
            captured_by=context.user_id,
            captured_at=utc_now(),
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return rights_snapshot_response(snapshot)

    @created_app.get(
        "/projects/{project_id}/asset-versions/{asset_version_id}/rights-check",
    )
    def check_asset_rights(
        project_id: str,
        asset_version_id: str,
        purpose: str | None = Query(default=None, max_length=120),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        asset_for_tenant(db, project_id, asset_version_id, context)
        snapshot = latest_rights_snapshot(db, asset_version_id, purpose)
        return {
            "assetVersionId": asset_version_id,
            **rights_decision(snapshot, utc_now()),
            "snapshot": rights_snapshot_response(snapshot) if snapshot else None,
        }

    def proxy_video_use_stream(
        path: str,
        request: Request,
        media_type: str = "application/octet-stream",
    ):
        forwarded_headers = {
            header: request.headers[header]
            for header in ("range", "if-range")
            if header in request.headers
        }
        stream_context = created_app.state.video_use.stream(
            path,
            headers=forwarded_headers or None,
        )
        try:
            upstream = stream_context.__enter__()
        except VideoUseError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "VIDEO_USE_UNAVAILABLE",
                    "message": "视频处理服务暂时不可用",
                },
            ) from exc

        response_headers = {
            header: upstream.headers[header]
            for header in (
                "accept-ranges",
                "cache-control",
                "content-disposition",
                "content-length",
                "content-range",
            )
            if header in upstream.headers
        }

        def body():
            try:
                yield from upstream.iter_bytes()
            finally:
                stream_context.__exit__(None, None, None)

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", media_type),
            headers=response_headers,
        )

    @created_app.get("/video-use/media/{project_id}/{media_id}")
    def stream_project_media(project_id: str, media_id: str, request: Request, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        project = project_for_tenant(db, project_id, context)
        material = next((item for item in project.materials if item.get("id") == media_id), None)
        if material is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND", "message": "未找到该媒体文件"})
        media_type = material.get("contentType") or ("video/mp4" if material.get("type") == "video" else "audio/mpeg")
        return proxy_video_use_stream(f"/media/{project_id}/{media_id}", request, media_type)

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
            raise HTTPException(status_code=429, detail={"code": "RENDER_CONCURRENCY_QUOTA_EXCEEDED", "message": "同时渲染的任务数已达到上限"})
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == context.tenant_id, DBTenant.render_seconds_used + reserved_seconds <= DBTenant.monthly_render_seconds_quota)
            .values(render_seconds_used=DBTenant.render_seconds_used + reserved_seconds, updated_at=utc_now())
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "RENDER_SECONDS_QUOTA_EXCEEDED", "message": "本月可用渲染时长已用完"})
        now = utc_now()
        task_id = str(uuid.uuid4())
        payload["requestId"] = task_id
        task = DBRenderTask(
            id=task_id, tenant_id=context.tenant_id, project_id=project_id,
            requested_by=context.user_id, status="QUEUED", progress=0,
            message="任务已进入渲染队列", engine="video-use", render_payload=payload,
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
    def stream_render_artifact(task_id: str, request: Request, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        task = db.execute(select(DBRenderTask).where(DBRenderTask.id == task_id, DBRenderTask.tenant_id == context.tenant_id)).scalar_one_or_none()
        if task is None or canonical_task_status(task.status) != "SUCCEEDED" or not task.upstream_job_id:
            raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND", "message": "未找到可下载的渲染成片"})
        return proxy_video_use_stream(f"/jobs/{task.upstream_job_id}/artifact", request, "video/mp4")

    @created_app.post("/internal/render-tasks/recover")
    def recover_tasks(
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        now = utc_now()
        stale = list(db.execute(select(DBRenderTask).where(DBRenderTask.status.in_(database_status_values("RUNNING")), DBRenderTask.lease_expires_at < now)).scalars())
        recovered = []
        for task in stale:
            task.status = "RUNNING" if task.upstream_job_id else "QUEUED"
            task.lease_owner = None
            task.lease_expires_at = None
            task.message = "工作节点租约过期后已恢复任务"
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
                DBRenderTask.status.in_(database_status_values("QUEUED", "RUNNING")),
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
        if not task.upstream_job_id:
            task.status = "RUNNING"
            task.attempts += 1
        else:
            task.status = "RUNNING"
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
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "未找到该任务"})
        if task.lease_owner != x_worker_id:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去该任务租约"})
        now = utc_now()
        if req.upstreamJobId:
            task.upstream_job_id = req.upstreamJobId
        task.progress = req.progress
        task.message = req.message
        task.error = req.error
        if req.status == "FAILED" and req.retryable and task.attempts < task.max_attempts:
            task.status = "QUEUED"
            task.upstream_job_id = None
            task.lease_owner = None
            task.lease_expires_at = None
        else:
            task.status = req.status
            if req.status in {"SUCCEEDED", "FAILED", "CANCELED", "PARTIAL"}:
                task.completed_at = now
                task.lease_owner = None
                task.lease_expires_at = None
            else:
                task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        task.updated_at = now
        if req.status == "SUCCEEDED":
            existing_candidate = db.execute(
                select(DBCandidate).where(DBCandidate.task_id == task.id)
            ).scalar_one_or_none()
            if existing_candidate is None:
                project = db.execute(
                    select(DBProject).where(DBProject.id == task.project_id)
                ).scalar_one()
                db.add(
                    DBCandidate(
                        id=str(uuid.uuid4()),
                        tenant_id=task.tenant_id,
                        project_id=task.project_id,
                        task_id=task.id,
                        artifact_ref=f"/api/renders/{task.id}/artifact",
                        input_revision=project.revision,
                        status="READY",
                        created_at=now,
                    )
                )
        db.commit()
        return task_response(task)

    @created_app.get("/projects/{project_id}/candidates")
    def list_candidates(
        project_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        candidates = db.execute(
            select(DBCandidate)
            .where(
                DBCandidate.project_id == project_id,
                DBCandidate.tenant_id == context.tenant_id,
            )
            .order_by(DBCandidate.created_at.desc())
        ).scalars()
        return [candidate_response(candidate) for candidate in candidates]

    @created_app.post(
        "/projects/{project_id}/candidates/{candidate_id}/adopt",
        status_code=201,
    )
    def adopt_candidate(
        project_id: str,
        candidate_id: str,
        req: AdoptCandidateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        project_for_tenant(db, project_id, context)
        if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "IDEMPOTENCY_KEY_REQUIRED",
                    "message": "采用候选需要 8–128 字符的幂等键",
                },
            )
        existing = db.execute(
            select(DBAdoption).where(
                DBAdoption.tenant_id == context.tenant_id,
                DBAdoption.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.project_id != project_id or existing.candidate_id != candidate_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "IDEMPOTENCY_KEY_REUSED",
                        "message": "该幂等键已用于其他采用操作",
                    },
                )
            master = db.execute(
                select(DBMasterRevision).where(DBMasterRevision.adoption_id == existing.id)
            ).scalar_one()
            return master_response(master, existing)

        candidate = db.execute(
            select(DBCandidate).where(
                DBCandidate.id == candidate_id,
                DBCandidate.project_id == project_id,
                DBCandidate.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if candidate is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "CANDIDATE_NOT_FOUND", "message": "未找到该候选成片"},
            )
        if candidate.status != "READY":
            raise HTTPException(
                status_code=409,
                detail={"code": "CANDIDATE_NOT_ADOPTABLE", "message": "候选当前不可采用"},
            )

        supersedes = None
        if req.supersedesId:
            supersedes = db.execute(
                select(DBAdoption).where(
                    DBAdoption.id == req.supersedesId,
                    DBAdoption.project_id == project_id,
                    DBAdoption.tenant_id == context.tenant_id,
                )
            ).scalar_one_or_none()
            if supersedes is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "SUPERSEDED_ADOPTION_NOT_FOUND", "message": "未找到被替代的采用记录"},
                )

        task = db.execute(
            select(DBRenderTask).where(
                DBRenderTask.id == candidate.task_id,
                DBRenderTask.tenant_id == context.tenant_id,
            )
        ).scalar_one()
        media_ids = {
            str(clip.get("materialId"))
            for track in (task.render_payload.get("canonicalTimeline", {}).get("tracks") or [])
            for clip in (track.get("clips") or [])
            if clip.get("materialId")
        }
        rights_failures = []
        for media_id in sorted(media_ids):
            asset = db.execute(
                select(DBAssetVersion)
                .where(
                    DBAssetVersion.project_id == project_id,
                    DBAssetVersion.tenant_id == context.tenant_id,
                    DBAssetVersion.media_id == media_id,
                )
                .order_by(DBAssetVersion.version_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            if asset is None:
                rights_failures.append({"mediaId": media_id, "code": "ASSET_VERSION_MISSING"})
                continue
            decision = rights_decision(
                latest_rights_snapshot(db, asset.id, "EXPORT"),
                utc_now(),
            )
            if not decision["allowed"]:
                rights_failures.append(
                    {"mediaId": media_id, "assetVersionId": asset.id, "code": decision["code"]}
                )
        if rights_failures:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "RIGHTS_CHECK_FAILED",
                    "message": "候选采用前的资产权利检查未通过",
                    "failures": rights_failures,
                },
            )

        now = utc_now()
        revision_no = (
            db.execute(
                select(func.max(DBMasterRevision.revision_no)).where(
                    DBMasterRevision.project_id == project_id,
                )
            ).scalar_one_or_none()
            or 0
        ) + 1
        adoption = DBAdoption(
            id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            project_id=project_id,
            candidate_id=candidate.id,
            adopted_by=context.user_id,
            adopted_at=now,
            reason=req.reason.strip(),
            supersedes_id=supersedes.id if supersedes else None,
            idempotency_key=idempotency_key,
        )
        master = DBMasterRevision(
            id=str(uuid.uuid4()),
            tenant_id=context.tenant_id,
            project_id=project_id,
            adoption_id=adoption.id,
            revision_no=revision_no,
            artifact_ref=candidate.artifact_ref,
            sha256=None,
            created_at=now,
        )
        candidate.status = "ADOPTED"
        db.add_all([adoption, master])
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            concurrent = db.execute(
                select(DBAdoption).where(
                    DBAdoption.tenant_id == context.tenant_id,
                    DBAdoption.idempotency_key == idempotency_key,
                )
            ).scalar_one_or_none()
            if (
                concurrent is not None
                and concurrent.project_id == project_id
                and concurrent.candidate_id == candidate_id
            ):
                concurrent_master = db.execute(
                    select(DBMasterRevision).where(
                        DBMasterRevision.adoption_id == concurrent.id,
                    )
                ).scalar_one()
                return master_response(concurrent_master, concurrent)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ADOPTION_CONFLICT",
                    "message": "候选已经被采用或母版修订发生并发冲突",
                },
            ) from exc
        return master_response(master, adoption)

    @created_app.get("/projects/{project_id}/masters")
    def list_master_revisions(
        project_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project_for_tenant(db, project_id, context)
        rows = db.execute(
            select(DBMasterRevision, DBAdoption)
            .join(DBAdoption, DBAdoption.id == DBMasterRevision.adoption_id)
            .where(
                DBMasterRevision.project_id == project_id,
                DBMasterRevision.tenant_id == context.tenant_id,
            )
            .order_by(DBMasterRevision.revision_no.desc())
        ).all()
        return [master_response(master, adoption) for master, adoption in rows]

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
