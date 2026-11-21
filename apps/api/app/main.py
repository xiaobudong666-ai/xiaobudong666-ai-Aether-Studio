from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
import math
import os
import re
import uuid
from collections.abc import Callable, Generator
from contextlib import asynccontextmanager, contextmanager

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
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, TimeoutError as SQLAlchemyPoolTimeout
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
    DBDirectMediaUploadHold,
    DBCandidate,
    DBGenerationAttempt,
    DBGenerationCircuitState,
    DBGenerationEvent,
    DBGenerationProviderAttestation,
    DBGenerationProviderConfigVersion,
    DBGenerationTask,
    DBGenerationUsageEntry,
    DBMasterRevision,
    DBProject,
    DBRenderTask,
    DBRightsSnapshot,
    DBSession,
    DBTenant,
    DBUser,
)
from .generation_tasks import (
    ACTIVE_GENERATION_STATES,
    DEFAULT_FAKE_POLICY,
    MONEYPRINTER_ADAPTER_VERSION,
    MONEYPRINTER_UPSTREAM_PIN,
    RETRYABLE_GENERATION_STATES,
    TERMINAL_GENERATION_STATES,
    WORKER_TRANSITIONS,
    add_event as add_generation_event,
    add_provider_event,
    add_usage_entry,
    build_capability_snapshot,
    capability_snapshot_valid,
    current_attempt as current_generation_attempt,
    provider_config_response,
    quota_summary,
    request_hash as generation_request_hash,
    safe_error_message,
    sha256_json,
    task_response as generation_task_response,
)
from .schemas import (
    AdoptCandidateRequest,
    ApplyTalkingHeadDraftRequest,
    CreateProjectRequest,
    DetectionEvidenceRequest,
    CreateRightsSnapshotRequest,
    CreateUserRequest,
    GenerationTaskRequest,
    TalkingHeadFakeGenerationRequest,
    GenerationProviderAttestationRequest,
    GenerationProviderKillSwitchRequest,
    GenerationProviderPolicyRequest,
    GenerationWorkerHeartbeatRequest,
    GenerationWorkerTransitionRequest,
    RecoverGenerationArtifactRequest,
    LoginRequest,
    ProjectResponse,
    UpdateProjectRequest,
    WorkerTaskUpdateRequest,
)
from .task_status import (
    canonical_task_status,
    database_status_values,
    legacy_task_status,
)
from .talking_head_planner import SubtitleCue, build_talking_head_timeline
from .detection_evidence import build_release_evidence, normalize_rights_evidence
from .runtime_gates import enforce_release_gates, enforce_timeline_receipt
from .timeline_render import build_render_payload
from .video_use_adapter import VideoUseAdapter, VideoUseError, VideoUseNotFoundError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api.main")

DatabaseDependency = Callable[[], Generator[Session, None, None]]
ACTIVE_TASK_STATES = database_status_values("QUEUED", "RUNNING")
PRIVATE_CANARY_PROFILE = "private-one-task-v1"


def private_canary_policy_valid(policy: dict) -> bool:
    """Return true only for the bounded, pinned one-task canary policy."""
    # G73: persisted policy values are untrusted until checked for exact types.
    # int(True), int("1") and int(1.9) would silently turn a malformed policy
    # into a valid one-task canary and could permit an unintended Worker lease.
    # This is read-only validation: do not normalize or persist Owner policy.
    if not isinstance(policy, dict):
        return False
    return (
        policy.get("enabledIntent") is True
        and type(policy.get("concurrentTaskLimit")) is int
        and policy["concurrentTaskLimit"] == 1
        and type(policy.get("monthlyRequestLimit")) is int
        and policy["monthlyRequestLimit"] == 1
        and type(policy.get("monthlyGeneratedSecondsLimit")) is int
        and 1 <= policy["monthlyGeneratedSecondsLimit"] <= 10
        and type(policy.get("maxClipDurationSeconds")) is int
        and 1 <= policy["maxClipDurationSeconds"] <= 10
        and type(policy.get("maxOutputs")) is int
        and policy["maxOutputs"] == 1
        and policy.get("artifactPathPrefixes") == ["/tasks/"]
        and type(policy.get("maxArtifactBytes")) is int
        and policy["maxArtifactBytes"] > 0
    )


def generation_failure_policy_valid(snapshot: dict) -> bool:
    """Reject corrupt persisted circuit bounds without normalizing Owner policy.

    Legacy snapshots lacking one of these fields retain the established
    defaults. Explicit bool/string/container/zero values do not become a
    Worker lease or an unhandled provider-failure transition.
    """
    if not isinstance(snapshot, dict):
        return False
    policy = snapshot.get("providerPolicy", {})
    if not isinstance(policy, dict):
        return False
    for field, fallback in (
        ("failureWindow", 300),
        ("failureThreshold", 5),
        ("cooldownSeconds", 60),
    ):
        value = policy.get(field, fallback)
        if type(value) is not int or value < 1:
            return False
        # G73: even an exact positive integer can exceed timedelta/datetime
        # representability when poisoned in a persisted task. Reject it before
        # a Worker lease or failure callback; never clamp Owner policy silently.
        try:
            interval = datetime.timedelta(seconds=value)
            reference = datetime.datetime.now(datetime.timezone.utc)
            if field == "failureWindow":
                reference - interval
            elif field == "cooldownSeconds":
                reference + interval
        except OverflowError:
            return False
    return True


def generation_prior_history_safe(db: Session, task: DBGenerationTask) -> bool:
    """Fail closed on damaged/paid earlier attempts, including post-claim drift.

    The same read-only durable invariant binds both Worker lease admission and
    subsequent Worker transitions. A clean FAILED/NOT_SUBMITTED prefix is
    allowed; missing, renumbered, future or billable history is not a STOP ACK.
    """
    if type(task.attempts) is not int or task.attempts < 1:
        return False
    history = list(db.execute(
        select(
            DBGenerationAttempt.attempt_no,
            DBGenerationAttempt.status,
            DBGenerationAttempt.reconciliation_state,
            DBGenerationAttempt.submission_started_at,
            DBGenerationAttempt.upstream_job_id,
        ).where(
            DBGenerationAttempt.generation_task_id == task.id,
            DBGenerationAttempt.attempt_no != task.attempts,
        ).order_by(DBGenerationAttempt.attempt_no)
    ).all())
    return (
        len(history) == task.attempts - 1
        and all(
            number == expected
            and status == "FAILED"
            and reconciliation == "NOT_SUBMITTED"
            and started_at is None
            and upstream_id is None
            for expected, (number, status, reconciliation, started_at, upstream_id)
            in enumerate(history, start=1)
        )
    )


def generation_failure_history_valid(history: object) -> bool:
    """Treat damaged durable circuit history as untrusted, without rewriting it.

    A scalar/container record can raise during failure reporting, while a
    timezone-naive timestamp cannot be compared to an aware UTC cutoff.
    Ignoring damaged entries would also discard known failure evidence and
    could keep an unsafe circuit closed. Refuse Worker claims and callbacks.
    """
    if history is None:
        return True  # Legacy circuit rows may have no failure timestamps.
    if not isinstance(history, list):
        return False
    for raw in history:
        if not isinstance(raw, str):
            return False
        try:
            parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                return False
        except (ValueError, OverflowError):
            return False
    return True


def attestation_canary_proof(
    attestation: DBGenerationProviderAttestation | None,
) -> tuple[str, str, str]:
    capabilities = (
        attestation.capabilities_json
        if attestation is not None and isinstance(attestation.capabilities_json, dict)
        else {}
    )
    return (
        str(capabilities.get("credentialState") or "ABSENT"),
        str(capabilities.get("networkIsolation") or "NOT_ENFORCED"),
        str(capabilities.get("canaryProfile") or "disabled"),
    )


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


GENERATION_TASK_LIST_STATUSES = {
    "QUEUED", "SUBMITTING", "RUNNING", "INGESTING", "RIGHTS_BLOCKED",
    "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN", "PARTIAL",
}
GENERATION_TASK_LIST_SORTS = {"createdAt:desc", "createdAt:asc"}


def generation_task_list_scope_hash(
    *,
    tenant_id: str,
    project_id: str,
    status: str | None,
    provider: str | None,
    requested_by: str | None,
    created_after: datetime.datetime | None,
    created_before: datetime.datetime | None,
    error_code: str | None,
    sort: str,
) -> str:
    """Bind a keyset cursor to its tenant, project, filters and fixed sort contract."""
    def normalized_time(value: datetime.datetime | None) -> str | None:
        current = _as_utc(value)
        return current.isoformat().replace("+00:00", "Z") if current else None

    return sha256_json({
        "tenantId": tenant_id,
        "projectId": project_id,
        "status": status,
        "provider": provider,
        "requestedBy": requested_by,
        "createdAfter": normalized_time(created_after),
        "createdBefore": normalized_time(created_before),
        "errorCode": error_code,
        "sort": sort,
        "tieBreaker": "taskId",
        "version": 1,
    })


def encode_generation_task_cursor(task: DBGenerationTask, scope_hash: str) -> str:
    created_at = _as_utc(task.created_at)
    payload = {
        "version": 1,
        "createdAt": created_at.isoformat().replace("+00:00", "Z"),
        "taskId": task.id,
        "scopeHash": scope_hash,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_generation_task_cursor(
    token: str,
    *,
    expected_scope_hash: str,
) -> tuple[datetime.datetime, str]:
    """Decode a continuation marker; authorization is always rechecked by the route."""
    if (
        not token
        or len(token) > 2048
        or any(not (character.isalnum() or character in "-_") for character in token)
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "GENERATION_CURSOR_INVALID", "message": "任务分页游标格式无效"},
        )
    try:
        padded = token + ("=" * (-len(token) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "createdAt", "taskId", "scopeHash"}
            or payload.get("version") != 1
            or not isinstance(payload.get("createdAt"), str)
            or not isinstance(payload.get("taskId"), str)
            or not payload["taskId"]
            or len(payload["taskId"]) > 128
            or not isinstance(payload.get("scopeHash"), str)
        ):
            raise ValueError("invalid cursor payload")
        created_at = datetime.datetime.fromisoformat(
            payload["createdAt"].replace("Z", "+00:00")
        )
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=422,
            detail={"code": "GENERATION_CURSOR_INVALID", "message": "任务分页游标内容无效"},
        )
    if payload["scopeHash"] != expected_scope_hash:
        raise HTTPException(
            status_code=409,
            detail={"code": "GENERATION_CURSOR_SCOPE_MISMATCH", "message": "筛选条件或项目已变化，请重置分页"},
        )
    return created_at.astimezone(datetime.timezone.utc), payload["taskId"]


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
    governance_gates: bool | None = None,
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
    if generation_provider_mode is None:
        resolved_generation_provider_mode = os.environ.get(
            "AETHER_GENERATION_PROVIDER_MODE", "disabled"
        )
        # FAKE-only routing is opt-in for disposable nonproduction Compose tests.
        # A stray provider-mode environment variable must remain fail-closed.
        if resolved_generation_provider_mode == "deterministic-fake":
            if (
                os.environ.get("AETHER_FAKE_ONLY_NONPROD", "").lower() == "true"
                and os.environ.get("AETHER_RUNTIME_STAGE", "").lower() == "nonproduction"
            ):
                pass
            else:
                resolved_generation_provider_mode = "disabled"
        elif resolved_generation_provider_mode not in {"disabled", "moneyprinter"}:
            resolved_generation_provider_mode = "disabled"
    else:
        resolved_generation_provider_mode = generation_provider_mode
        if resolved_generation_provider_mode not in {
            "disabled", "deterministic-fake", "moneyprinter"
        }:
            resolved_generation_provider_mode = "disabled"
    resolved_governance_gates = (
        os.environ.get("AETHER_GOVERNANCE_GATES", "false").lower() == "true"
        if governance_gates is None
        else governance_gates
    )

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
    created_app.state.video_use = video_use_adapter or VideoUseAdapter()
    created_app.state.generation_provider_mode = resolved_generation_provider_mode
    created_app.state.governance_gates = resolved_governance_gates
    created_app.state.setup_required = False

    @created_app.exception_handler(SQLAlchemyPoolTimeout)
    async def database_pool_capacity_unavailable(_request: Request, _exc: SQLAlchemyPoolTimeout):
        """Fail closed when an initial request DB checkout exhausts the pool.

        Liveness/probe checkouts already return bounded 503; an earlier
        SQLAlchemy Session checkout must not leak an unhandled HTTP 500.
        No quota, asset, release receipt or Provider action is implied.
        """
        return JSONResponse(
            status_code=503,
            content={"detail": {
                "code": "DATABASE_CAPACITY_UNAVAILABLE",
                "message": "数据库连接暂不可用，请稍后重试",
            }},
        )

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
        *,
        for_update: bool = False,
    ) -> DBAssetVersion:
        statement = select(DBAssetVersion).where(
            DBAssetVersion.id == asset_version_id,
            DBAssetVersion.project_id == project_id,
            DBAssetVersion.tenant_id == context.tenant_id,
        )
        if for_update and app_engine.dialect.name != "sqlite":
            statement = statement.with_for_update()
        asset = db.execute(statement).scalar_one_or_none()
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
        decision = rights_decision(
            latest_rights_snapshot(db, task.asset_version_id, "EXPORT"),
            utc_now(),
        )
        # Human-entered rights snapshots are evidence records, not trusted
        # M12 export grants. The current model has no verifiable issuer/signature
        # binding. Until the Owner-approved issuer contract is implemented,
        # a caller-selected ALLOWED snapshot must never authorize generated use.
        if decision.get("allowed"):
            return {
                "allowed": False,
                "code": "RIGHTS_TRUSTED_ISSUER_REQUIRED",
            }
        return decision

    def fake_talking_head_creation_evidence_matches(
        db: Session, task: DBGenerationTask, request: dict
    ) -> bool:
        """Bind a FAKE-only Worker task to its original append-only create receipt.

        The request row may be edited after creation/claim. A self-consistent
        replacement image ID + SHA + version list is not sufficient proof of
        the image the authenticated user actually submitted.
        """
        source_id = request.get("imageAssetId")
        source_sha = request.get("sourceAssetSha256")
        if (
            not isinstance(source_id, str) or not source_id
            or not isinstance(source_sha, str) or not source_sha
            or request.get("imageAssetVersionId") != source_id
        ):
            return False
        # G72: the authenticated typed request was hashed before internal
        # FAKE routing fields were appended. The immutable image receipt
        # alone does not bind the script/duration or other user choices.
        # Reject changed persisted user fields at Worker claim, pre-upload
        # and final intake without writing usage, assets or audit storage.
        original_request_fields = {
            field: request.get(field)
            for field in TalkingHeadFakeGenerationRequest.model_fields
        }
        if generation_request_hash(original_request_fields) != task.request_hash:
            return False
        # G72: typed user choices are copied into Worker-facing fields at
        # creation. Those derived values are NOT part of the typed request
        # hash. A corrupted row could leave text/duration intact while
        # changing the actual video sent to the FAKE Worker. Bind the copies
        # to the same original choices before claim, upload and final intake.
        if (
            request.get("videoSubject") != original_request_fields["text"]
            or request.get("videoAspect") != "9:16"
            or type(request.get("videoClipDuration")) is not int
            or request.get("videoClipDuration") != original_request_fields["durationSeconds"]
            or request.get("voiceId") != "synthetic-seed"
        ):
            return False
        created = db.execute(
            select(DBGenerationEvent)
            .where(
                DBGenerationEvent.generation_task_id == task.id,
                DBGenerationEvent.event_type == "TASK_CREATED",
            )
            .order_by(DBGenerationEvent.created_at.asc(), DBGenerationEvent.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            created is None
            or created.actor_type != "USER"
            or created.actor_id != task.requested_by
            or not isinstance(created.metadata_json, dict)
        ):
            return False
        evidence = created.metadata_json
        return (
            evidence.get("fakeOnly") is True
            and evidence.get("simulatedOutput") is True
            # Defense in depth only: unlike row-local request_hash, this is
            # a second event record. It is NOT an independently immutable
            # Owner-approved trusted issuer/receipt and cannot close that gate.
            and evidence.get("typedRequestHash") == task.request_hash
            and evidence.get("inputAssetVersionId") == source_id
            and evidence.get("sourceAssetSha256") == source_sha
        )

    def generated_asset_export_rights(
        db: Session, asset: DBAssetVersion
    ) -> dict | None:
        generated_task = db.execute(
            select(DBGenerationTask)
            .where(
                DBGenerationTask.project_id == asset.project_id,
                DBGenerationTask.tenant_id == asset.tenant_id,
                DBGenerationTask.asset_version_id == asset.id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if generated_task is not None:
            return generation_rights(db, generated_task)
        provenance = (
            asset.probe_json.get("provenance")
            if isinstance(asset.probe_json, dict)
            else None
        )
        if isinstance(provenance, dict) and provenance.get("generationTaskId"):
            return {"allowed": False, "code": "GENERATION_TASK_MISSING"}
        return None

    def generation_task_by_id(
        db: Session, task_id: str, *, for_update: bool = False
    ) -> DBGenerationTask | None:
        statement = select(DBGenerationTask).where(DBGenerationTask.id == task_id)
        if for_update and app_engine.dialect.name != "sqlite":
            statement = statement.with_for_update()
        return db.execute(statement).scalar_one_or_none()

    def generation_task_for_tenant(
        db: Session,
        project_id: str,
        task_id: str,
        context: AuthContext,
        *,
        for_update: bool = False,
    ) -> DBGenerationTask:
        statement = select(DBGenerationTask).where(
            DBGenerationTask.id == task_id,
            DBGenerationTask.project_id == project_id,
            DBGenerationTask.tenant_id == context.tenant_id,
        )
        if for_update and app_engine.dialect.name != "sqlite":
            statement = statement.with_for_update()
        task = db.execute(statement).scalar_one_or_none()
        if task is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"},
            )
        return task

    def published_generation_config(
        db: Session, tenant_id: str
    ) -> DBGenerationProviderConfigVersion | None:
        return db.execute(
            select(DBGenerationProviderConfigVersion)
            .where(
                DBGenerationProviderConfigVersion.tenant_id == tenant_id,
                DBGenerationProviderConfigVersion.provider == "moneyprinter",
                DBGenerationProviderConfigVersion.status == "PUBLISHED",
            )
            .order_by(DBGenerationProviderConfigVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    def generation_circuit(
        db: Session, tenant_id: str, *, create: bool = False, for_update: bool = False
    ) -> DBGenerationCircuitState | None:
        # A SELECT FOR UPDATE cannot lock a row that does not exist. Circuit
        # creation therefore uses a unique-key upsert on PostgreSQL/SQLite
        # before the locking read. Concurrent Owner stop and artifact finalization
        # then serialize on the same durable tenant/provider row.
        if create:
            values = {
                "id": str(uuid.uuid4()),
                "tenant_id": tenant_id,
                "provider": "moneyprinter",
                "state": "CLOSED",
                "failure_timestamps_json": [],
                "updated_at": utc_now(),
            }
            if app_engine.dialect.name == "postgresql":
                db.execute(
                    pg_insert(DBGenerationCircuitState)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            DBGenerationCircuitState.tenant_id,
                            DBGenerationCircuitState.provider,
                        ]
                    )
                )
            elif app_engine.dialect.name == "sqlite":
                db.execute(
                    sqlite_insert(DBGenerationCircuitState)
                    .values(**values)
                    .on_conflict_do_nothing(
                        index_elements=[
                            DBGenerationCircuitState.tenant_id,
                            DBGenerationCircuitState.provider,
                        ]
                    )
                )
            else:
                existing = db.execute(
                    select(DBGenerationCircuitState).where(
                        DBGenerationCircuitState.tenant_id == tenant_id,
                        DBGenerationCircuitState.provider == "moneyprinter",
                    )
                ).scalar_one_or_none()
                if existing is None:
                    db.add(DBGenerationCircuitState(**values))
                    db.flush()

        statement = select(DBGenerationCircuitState).where(
            DBGenerationCircuitState.tenant_id == tenant_id,
            DBGenerationCircuitState.provider == "moneyprinter",
        )
        if for_update and app_engine.dialect.name != "sqlite":
            statement = statement.with_for_update()
        return db.execute(statement).scalar_one_or_none()

    def current_generation_capabilities(db: Session, tenant_id: str) -> dict:
        now = utc_now()
        snapshot_time = now.replace(second=0, microsecond=0)
        mode = created_app.state.generation_provider_mode
        config = published_generation_config(db, tenant_id)
        circuit = generation_circuit(db, tenant_id, for_update=True)
        circuit_state = circuit.state if circuit is not None else "CLOSED"
        # A persisted published policy can be malformed in legacy/partially recovered data.
        # Never let corrupt JSON crash readiness or authorize a Provider claim.
        config_policy = config.policy_json if config is not None and isinstance(config.policy_json, dict) else None
        policy = config_policy if config_policy is not None else DEFAULT_FAKE_POLICY
        reason_code: str | None = None
        attestation: DBGenerationProviderAttestation | None = None
        healthy = True

        if mode == "disabled":
            reason_code = "OPERATOR_DISABLED"
            healthy = False
        elif mode == "moneyprinter":
            if config is None:
                reason_code = "OWNER_CONFIG_MISSING"
                healthy = False
            elif config_policy is None:
                reason_code = "OWNER_POLICY_INVALID"
                healthy = False
            elif not bool(config_policy.get("enabledIntent")):
                reason_code = "OWNER_POLICY_DISABLED"
                healthy = False
            else:
                attestation = db.execute(
                    select(DBGenerationProviderAttestation)
                    .where(
                        DBGenerationProviderAttestation.provider == "moneyprinter",
                        DBGenerationProviderAttestation.tenant_id == tenant_id,
                        DBGenerationProviderAttestation.config_version_id == config.id,
                    )
                    .order_by(DBGenerationProviderAttestation.checked_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if attestation is None:
                    reason_code = "WORKER_ATTESTATION_MISSING"
                    healthy = False
                elif _as_utc(attestation.expires_at) <= now:
                    reason_code = "WORKER_ATTESTATION_EXPIRED"
                    healthy = False
                elif attestation.operator_mode != "moneyprinter":
                    reason_code = "WORKER_OPERATOR_MODE_MISMATCH"
                    healthy = False
                elif attestation.policy_hash != config.policy_hash:
                    reason_code = "WORKER_POLICY_MISMATCH"
                    healthy = False
                elif attestation.upstream_pin != MONEYPRINTER_UPSTREAM_PIN:
                    reason_code = "WORKER_UPSTREAM_PIN_MISMATCH"
                    healthy = False
                elif attestation_canary_proof(attestation)[0] != "PRESENT":
                    reason_code = "WORKER_CREDENTIAL_STATE_INVALID"
                    healthy = False
                elif attestation_canary_proof(attestation)[1] != "ENFORCED":
                    reason_code = "WORKER_NETWORK_ISOLATION_MISSING"
                    healthy = False
                elif attestation_canary_proof(attestation)[2] != PRIVATE_CANARY_PROFILE:
                    reason_code = "WORKER_CANARY_PROFILE_MISMATCH"
                    healthy = False
                elif not private_canary_policy_valid(config_policy):
                    reason_code = "CANARY_POLICY_MISMATCH"
                    healthy = False
                elif not attestation.healthy:
                    reason_code = attestation.reason_code or "PROVIDER_UNHEALTHY"
                    healthy = False

        if circuit_state == "DISABLED":
            reason_code = circuit.disabled_reason_code or "OWNER_KILL_SWITCH"
            healthy = False
        elif circuit_state == "OPEN":
            cooldown_until = _as_utc(circuit.cooldown_until) if circuit else None
            if cooldown_until is not None and now >= cooldown_until:
                circuit.state = "HALF_OPEN"
                circuit.half_open_task_id = None
                circuit.half_open_lease_epoch = None
                circuit.half_open_issuer = None
                circuit.updated_at = now
                circuit_state = "HALF_OPEN"
                add_provider_event(
                    db, tenant_id=tenant_id, event_type="CIRCUIT_HALF_OPENED",
                    actor_type="SYSTEM", actor_id="api", now=now,
                )
                db.commit()
            else:
                reason_code = "GENERATION_CIRCUIT_OPEN"
                healthy = False

        # G73: a structurally corrupted *published* policy may have failed
        # the canary gate, but quota/snapshot serializers still cast its raw
        # fields. A disabled readiness response must be safe to construct.
        # Use synthetic fallback values for reporting only; never change the
        # persisted Owner policy, create a lease, or enable the Provider.
        if mode == "moneyprinter" and not private_canary_policy_valid(policy):
            policy = DEFAULT_FAKE_POLICY
        quota = quota_summary(db, tenant_id, policy, now)
        snapshot = build_capability_snapshot(
            mode if healthy else "disabled",
            snapshot_time,
            policy=policy,
            config_version_id=config.id if config else None,
            policy_hash=config.policy_hash if config else None,
            healthy=healthy,
            reason_code=reason_code,
            attestation_expires_at=_as_utc(attestation.expires_at) if attestation else None,
            quota=quota,
            circuit_state=circuit_state,
        )
        credential_state, network_isolation, canary_profile = attestation_canary_proof(
            attestation
        )
        snapshot.update({
            "operatorMode": mode,
            "credentialState": credential_state,
            "networkIsolation": network_isolation,
            "canaryProfile": canary_profile,
            "ownerPolicy": {
                "published": config is not None,
                "enabledIntent": bool(config_policy and config_policy.get("enabledIntent")),
            },
            "workerProof": {
                "present": attestation is not None,
                "fresh": bool(attestation and _as_utc(attestation.expires_at) > now),
                "checkedAt": iso_utc(attestation.checked_at) if attestation else None,
                "expiresAt": iso_utc(attestation.expires_at) if attestation else None,
                "adapterVersion": attestation.adapter_version if attestation else None,
                "upstreamPin": attestation.upstream_pin if attestation else None,
            },
            "killSwitch": {
                "disabled": circuit_state == "DISABLED",
                "reasonCode": circuit.disabled_reason_code if circuit and circuit.state == "DISABLED" else None,
            },
        })
        snapshot.pop("snapshotHash", None)
        snapshot["snapshotHash"] = sha256_json({
            key: value for key, value in snapshot.items() if key != "quota"
        })
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
        snapshot = current_generation_capabilities(db, context.tenant_id)
        valid, code = capability_snapshot_valid(
            snapshot, req.capabilitySnapshotHash, utc_now()
        )
        if not valid:
            status_code = 409 if code in {
                "CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED"
            } else 503
            raise HTTPException(
                status_code=status_code,
                detail={"code": code, "message": "生成服务能力快照不可用，请重新预检"},
            )
        if req.voiceName not in snapshot["voices"]:
            raise HTTPException(
                status_code=422,
                detail={"code": "VOICE_UNSUPPORTED", "message": "所选声音不在当前能力范围内"},
            )
        if req.videoAspect not in snapshot["videoAspects"]:
            raise HTTPException(
                status_code=422,
                detail={"code": "ASPECT_UNSUPPORTED", "message": "所选画幅不在当前能力范围内"},
            )
        if req.videoConcatMode not in snapshot["videoConcatModes"]:
            raise HTTPException(
                status_code=422,
                detail={"code": "CONCAT_MODE_UNSUPPORTED", "message": "所选拼接模式不在当前能力范围内"},
            )
        if req.videoClipDuration > int(snapshot["clipDurationSeconds"]["max"]):
            raise HTTPException(
                status_code=422,
                detail={"code": "CLIP_DURATION_UNSUPPORTED", "message": "片段时长超过当前能力范围"},
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

    def require_fake_talking_head_claim_authority(
        db: Session, project: DBProject, context: AuthContext,
    ) -> None:
        # Fail closed before any FAKE-only queue/usage write: the existing
        # Worker can claim only if the originating user is an active project
        # Owner. Do not invent editor grants or admin delegation while the
        # Owner's project authorization contract remains unapproved.
        if project.owner_id != context.user_id or context.role != "owner":
            raise HTTPException(
                status_code=403,
                detail={"code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权建立可领取的模拟口播任务"},
            )
        originator_id = db.execute(select(DBUser.id).where(
            DBUser.id == context.user_id,
            DBUser.tenant_id == context.tenant_id,
            DBUser.role == "owner",
            DBUser.is_active.is_(True),
        )).scalar_one_or_none()
        if originator_id is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权建立可领取的模拟口播任务"},
            )

    def validate_fake_talking_head_request(
        db: Session,
        project: DBProject,
        req: TalkingHeadFakeGenerationRequest,
        context: AuthContext,
    ) -> tuple[dict, DBAssetVersion]:
        require_fake_talking_head_claim_authority(db, project, context)
        # The real production/default mode is always disabled here.
        if created_app.state.generation_provider_mode != "deterministic-fake":
            raise HTTPException(
                status_code=503,
                detail={"code": "TALKING_HEAD_FAKE_DISABLED", "message": "仅允许隔离测试口播FAKE模式"},
            )
        if project.revision != req.expectedProjectRevision:
            raise HTTPException(
                status_code=409,
                detail={"code": "PROJECT_REVISION_CONFLICT", "message": "项目版本已变化，请重新预检"},
            )
        snapshot = current_generation_capabilities(db, context.tenant_id)
        if snapshot.get("mode") != "deterministic-fake" or not snapshot.get("enabled"):
            raise HTTPException(
                status_code=503,
                detail={"code": "TALKING_HEAD_FAKE_UNAVAILABLE", "message": "隔离测试环境生成服务不可用"},
            )
        valid, code = capability_snapshot_valid(snapshot, req.capabilitySnapshotHash, utc_now())
        if not valid:
            raise HTTPException(
                status_code=409 if code in {"CAPABILITY_SNAPSHOT_UNKNOWN", "CAPABILITY_SNAPSHOT_EXPIRED"} else 503,
                detail={"code": code, "message": "生成能力快照无效，请重新预检"},
            )
        asset = asset_for_tenant(db, project.id, req.imageAssetVersionId, context)
        if asset.media_type != "image" or not asset.sha256:
            raise HTTPException(
                status_code=422,
                detail={"code": "FAKE_VISUAL_ASSET_INVALID", "message": "需提供本租户本项目不可变图片资产版本"},
            )
        remix_rights = rights_decision(latest_rights_snapshot(db, asset.id, "REMIX"), utc_now())
        if not remix_rights["allowed"]:
            raise HTTPException(
                status_code=409,
                detail={"code": "FAKE_VISUAL_RIGHTS_BLOCKED", "message": "口播输入图片的使用授权不可用"},
            )
        return snapshot, asset

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

    def release_generation_reservation(
        db: Session,
        task: DBGenerationTask,
        attempt: DBGenerationAttempt,
        *,
        reason_code: str,
        now: datetime.datetime,
    ) -> None:
        settled = db.execute(
            select(DBGenerationUsageEntry.id).where(
                DBGenerationUsageEntry.task_id == task.id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )
        ).scalar_one_or_none()
        if settled is not None:
            return
        entry = add_usage_entry(
            db, task=task, attempt=attempt, kind="RELEASED",
            request_units=1, generated_seconds=0,
            reservation_key=f"generation:{task.id}",
            config_version_id=task.capability_snapshot_json.get("configVersionId"),
            now=now,
        )
        if entry is not None:
            add_generation_event(
                db, task, attempt=attempt, event_type="GENERATION_USAGE_RELEASED",
                actor_type="SYSTEM", actor_id="api",
                metadata={"reasonCode": reason_code}, now=now,
            )

    def record_generation_failure(
        db: Session,
        task: DBGenerationTask,
        *,
        now: datetime.datetime,
    ) -> None:
        if not generation_failure_policy_valid(task.capability_snapshot_json):
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_FAILURE_POLICY_INVALID", "message": "任务故障熔断参数异常，需隔离核验"},
            )
        policy = task.capability_snapshot_json.get("providerPolicy") or {}
        window_seconds = max(10, int(policy.get("failureWindow", 300)))
        threshold = max(1, int(policy.get("failureThreshold", 5)))
        cooldown_seconds = max(10, int(policy.get("cooldownSeconds", 60)))
        circuit = generation_circuit(db, task.tenant_id, create=True, for_update=True)
        if circuit.state == "DISABLED":
            return
        cutoff = now - datetime.timedelta(seconds=window_seconds)
        history = circuit.failure_timestamps_json
        if not generation_failure_history_valid(history):
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_CIRCUIT_HISTORY_INVALID",
                        "message": "持久化熔断历史无效，任务必须隔离核验"},
            )
        timestamps: list[datetime.datetime] = []
        for raw in history or []:
            parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed >= cutoff:
                timestamps.append(parsed)
        timestamps.append(now)
        previous = circuit.state
        should_open = previous == "HALF_OPEN" or len(timestamps) >= threshold
        circuit.failure_timestamps_json = [iso_utc(item) for item in timestamps]
        if should_open:
            circuit.state = "OPEN"
            circuit.opened_at = now
            circuit.cooldown_until = now + datetime.timedelta(seconds=cooldown_seconds)
            circuit.half_open_task_id = None
            circuit.half_open_lease_epoch = None
            circuit.half_open_issuer = None
            if previous != "OPEN":
                add_provider_event(
                    db, tenant_id=task.tenant_id, event_type="GENERATION_CIRCUIT_OPENED",
                    actor_type="SYSTEM", actor_id="api",
                    metadata={"failureCount": len(timestamps), "failureThreshold": threshold}, now=now,
                )
        circuit.updated_at = now

    def record_generation_success(
        db: Session, task: DBGenerationTask, *, issuer_id: str, now: datetime.datetime
    ) -> None:
        circuit = generation_circuit(db, task.tenant_id, for_update=True)
        # Close only for the exact durable probe lease: task, monotonic epoch,
        # and worker issuer must all match the currently persisted probe.
        if (
            circuit is None
            or circuit.state != "HALF_OPEN"
            or circuit.half_open_task_id != task.id
            or circuit.half_open_lease_epoch != task.lease_epoch
            or circuit.half_open_issuer != issuer_id
            or circuit.probe_epoch != task.circuit_probe_epoch
        ):
            return
        previous = circuit.state
        circuit.state = "CLOSED"
        circuit.failure_timestamps_json = []
        circuit.opened_at = None
        circuit.cooldown_until = None
        circuit.half_open_task_id = None
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        circuit.updated_at = now
        add_provider_event(
            db, tenant_id=task.tenant_id, event_type="GENERATION_CIRCUIT_CLOSED",
            actor_type="SYSTEM", actor_id="api",
            metadata={
                "fromState": previous, "taskId": task.id,
                "probeEpoch": circuit.probe_epoch, "leaseEpoch": task.lease_epoch,
                "issuer": issuer_id,
            }, now=now,
        )

    @created_app.get("/health")
    def health_check(db: Session = Depends(db_dependency)):
        # SQLite has PRAGMA journal_mode; PostgreSQL does not. A health probe
        # must verify the configured native database rather than failing with
        # a SQLite-only statement or misreporting the active backend.
        dialect = app_engine.dialect.name
        if dialect == "sqlite":
            journal_mode = db.execute(text("PRAGMA journal_mode;")).scalar_one().upper()
        else:
            db.execute(text("SELECT 1")).scalar_one()
            journal_mode = None
        return {
            "status": "healthy",
            "service": "api",
            "database": dialect,
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

    def legacy_moneyprinter_gone(task_id: str | None = None):
        del task_id
        raise HTTPException(
            status_code=410,
            detail={
                "code": "LEGACY_PROVIDER_ROUTE_RETIRED",
                "message": "旧 Provider 直连接口已退役，请使用受治理的项目级生成接口",
            },
        )

    created_app.add_api_route(
        "/moneyprinter/health", legacy_moneyprinter_gone, methods=["GET"]
    )
    created_app.add_api_route(
        "/moneyprinter/capabilities", legacy_moneyprinter_gone, methods=["GET"]
    )
    created_app.add_api_route(
        "/moneyprinter/generate", legacy_moneyprinter_gone, methods=["POST"]
    )
    created_app.add_api_route(
        "/moneyprinter/status/{task_id}", legacy_moneyprinter_gone, methods=["GET"]
    )

    def reject_unsafe_provider_value(value: object) -> None:
        secret_markers = (
            "api_key", "api-key", "secret", "token", "cookie",
            "authorization", "password", "credential",
        )
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key).lower().replace("_", "").replace("-", "")
                if (
                    any(marker.replace("_", "").replace("-", "") in normalized_key for marker in secret_markers)
                    or normalized_key in {"url", "uri", "host", "port", "baseurl", "apiurl"}
                    or normalized_key.endswith(("url", "uri", "host", "port"))
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "PROVIDER_POLICY_UNSAFE", "message": "Provider 策略包含禁止字段"},
                    )
                reject_unsafe_provider_value(item)
        elif isinstance(value, list):
            for item in value:
                reject_unsafe_provider_value(item)
        elif isinstance(value, str) and (
            "://" in value.lower()
            or any(marker in value.lower() for marker in secret_markers)
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "PROVIDER_POLICY_UNSAFE", "message": "Provider 策略包含疑似秘密或地址"},
            )

    @created_app.get("/generation/providers/moneyprinter/capabilities")
    def generation_capabilities(
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        return current_generation_capabilities(db, context.tenant_id)

    @created_app.get("/generation/providers/moneyprinter/readiness")
    def generation_readiness(
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        return current_generation_capabilities(db, context.tenant_id)

    @created_app.get("/generation/providers/moneyprinter/config-versions")
    def list_generation_provider_configs(
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner")
        configs = db.execute(
            select(DBGenerationProviderConfigVersion)
            .where(
                DBGenerationProviderConfigVersion.tenant_id == context.tenant_id,
                DBGenerationProviderConfigVersion.provider == "moneyprinter",
            )
            .order_by(DBGenerationProviderConfigVersion.version.desc())
        ).scalars()
        return [provider_config_response(config) for config in configs]

    @created_app.post(
        "/generation/providers/moneyprinter/config-versions", status_code=201
    )
    def create_generation_provider_config(
        req: GenerationProviderPolicyRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner")
        policy = req.model_dump(mode="json")
        reject_unsafe_provider_value(policy)
        begin_serialized_write(db)
        latest = db.execute(
            select(DBGenerationProviderConfigVersion)
            .where(
                DBGenerationProviderConfigVersion.tenant_id == context.tenant_id,
                DBGenerationProviderConfigVersion.provider == "moneyprinter",
            )
            .order_by(DBGenerationProviderConfigVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        now = utc_now()
        config = DBGenerationProviderConfigVersion(
            id=str(uuid.uuid4()), tenant_id=context.tenant_id,
            provider="moneyprinter", version=(latest.version + 1 if latest else 1),
            status="DRAFT", policy_json=policy, policy_hash=sha256_json(policy),
            created_by=context.user_id, created_at=now,
            supersedes_id=latest.id if latest else None,
        )
        db.add(config)
        add_provider_event(
            db, tenant_id=context.tenant_id, event_type="PROVIDER_CONFIG_CREATED",
            actor_type="USER", actor_id=context.user_id,
            metadata={"configVersionId": config.id, "version": config.version}, now=now,
        )
        db.commit()
        return provider_config_response(config)

    @created_app.post(
        "/generation/providers/moneyprinter/config-versions/{config_id}/publish"
    )
    def publish_generation_provider_config(
        config_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner")
        begin_serialized_write(db)
        config = db.execute(
            select(DBGenerationProviderConfigVersion).where(
                DBGenerationProviderConfigVersion.id == config_id,
                DBGenerationProviderConfigVersion.tenant_id == context.tenant_id,
                DBGenerationProviderConfigVersion.provider == "moneyprinter",
            )
        ).scalar_one_or_none()
        if config is None:
            raise HTTPException(status_code=404, detail={"code": "PROVIDER_CONFIG_NOT_FOUND", "message": "未找到 Provider 配置版本"})
        if config.status != "DRAFT":
            raise HTTPException(status_code=409, detail={"code": "PROVIDER_CONFIG_IMMUTABLE", "message": "配置版本已发布或被替代"})
        now = utc_now()
        previous = published_generation_config(db, context.tenant_id)
        if previous is not None and previous.id != config.id:
            previous.status = "SUPERSEDED"
        config.status = "PUBLISHED"
        config.published_by = context.user_id
        config.published_at = now
        add_provider_event(
            db, tenant_id=context.tenant_id, event_type="PROVIDER_CONFIG_PUBLISHED",
            actor_type="USER", actor_id=context.user_id,
            metadata={"configVersionId": config.id, "version": config.version,
                      "supersededConfigVersionId": previous.id if previous else None}, now=now,
        )
        db.commit()
        return provider_config_response(config)

    @created_app.post("/generation/providers/moneyprinter/kill-switch")
    def set_generation_kill_switch(
        req: GenerationProviderKillSwitchRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner")
        begin_serialized_write(db)
        now = utc_now()
        circuit = generation_circuit(db, context.tenant_id, create=True, for_update=True)
        previous = circuit.state
        circuit.state = "DISABLED" if req.disabled else "CLOSED"
        circuit.disabled_reason_code = req.reasonCode if req.disabled else None
        circuit.failure_timestamps_json = [] if not req.disabled else circuit.failure_timestamps_json
        circuit.opened_at = None if not req.disabled else circuit.opened_at
        circuit.cooldown_until = None if not req.disabled else circuit.cooldown_until
        circuit.half_open_task_id = None
        circuit.half_open_lease_epoch = None
        circuit.half_open_issuer = None
        circuit.updated_at = now
        add_provider_event(
            db, tenant_id=context.tenant_id,
            event_type="PROVIDER_EMERGENCY_STOPPED" if req.disabled else "PROVIDER_EMERGENCY_RECOVERED",
            actor_type="USER", actor_id=context.user_id,
            metadata={"fromState": previous, "toState": circuit.state, "reasonCode": req.reasonCode},
            now=now,
        )
        db.commit()
        return current_generation_capabilities(db, context.tenant_id)

    @created_app.post("/internal/generation/providers/moneyprinter/attest", status_code=201)
    def attest_generation_provider(
        req: GenerationProviderAttestationRequest,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        now = utc_now()
        checked_at = _as_utc(req.checkedAt)
        expires_at = _as_utc(req.expiresAt)
        if checked_at > now + datetime.timedelta(seconds=30) or expires_at <= now or expires_at > checked_at + datetime.timedelta(minutes=5):
            raise HTTPException(status_code=422, detail={"code": "WORKER_ATTESTATION_WINDOW_INVALID", "message": "Worker 证明有效期无效"})
        allowed_capability_keys = {
            "videoAspects", "voices", "videoConcatModes", "maxOutputs",
            "maxClipDurationSeconds", "cancellationSupported", "artifactStreaming",
        }
        if set(req.capabilities) - allowed_capability_keys:
            raise HTTPException(status_code=422, detail={"code": "WORKER_ATTESTATION_UNSAFE", "message": "Worker 证明包含未允许字段"})
        reject_unsafe_provider_value(req.capabilities)
        if req.healthy and (
            req.credentialState != "PRESENT"
            or req.networkIsolation != "ENFORCED"
            or req.canaryProfile != PRIVATE_CANARY_PROFILE
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "WORKER_CANARY_PROOF_INVALID",
                    "message": "Worker 私有金丝雀证明不完整",
                },
            )
        safe_capabilities = dict(req.capabilities)
        safe_capabilities.update({
            "credentialState": req.credentialState,
            "networkIsolation": req.networkIsolation,
            "canaryProfile": req.canaryProfile,
        })
        if req.tenantId is not None and req.configVersionId is not None:
            config = db.execute(
                select(DBGenerationProviderConfigVersion).where(
                    DBGenerationProviderConfigVersion.id == req.configVersionId,
                    DBGenerationProviderConfigVersion.tenant_id == req.tenantId,
                )
            ).scalar_one_or_none()
            if config is None:
                raise HTTPException(status_code=422, detail={"code": "WORKER_CONFIG_UNKNOWN", "message": "Worker 证明引用未知配置"})
        attestation = DBGenerationProviderAttestation(
            id=str(uuid.uuid4()), tenant_id=req.tenantId, provider=req.provider,
            worker_id=x_worker_id, operator_mode=req.operatorMode,
            config_version_id=req.configVersionId, policy_hash=req.policyHash,
            adapter_version=req.adapterVersion, upstream_pin=req.upstreamPin,
            healthy=req.healthy, capabilities_json=safe_capabilities,
            reason_code=req.reasonCode, checked_at=checked_at,
            expires_at=expires_at, created_at=now,
        )
        db.add(attestation)
        add_provider_event(
            db, tenant_id=req.tenantId, event_type="WORKER_ATTESTED",
            actor_type="WORKER", actor_id=x_worker_id,
            metadata={"configVersionId": req.configVersionId, "healthy": req.healthy,
                      "reasonCode": req.reasonCode, "expiresAt": iso_utc(expires_at)}, now=now,
        )
        db.commit()
        return {"accepted": True, "attestationId": attestation.id, "expiresAt": iso_utc(expires_at)}

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
            "readiness": {
                "enabled": snapshot["enabled"],
                "reasonCode": snapshot["reasonCode"],
                "circuit": snapshot["circuit"],
                "killSwitch": snapshot["killSwitch"],
            },
            "quota": snapshot["quota"],
        }

    @created_app.post("/projects/{project_id}/talking-head/fake-generation-tasks/validate")
    def validate_fake_talking_head_task(
        project_id: str,
        req: TalkingHeadFakeGenerationRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        project = project_for_tenant(db, project_id, context)
        snapshot, _asset = validate_fake_talking_head_request(db, project, req, context)
        return {
            "allowed": True, "status": "PREFLIGHT", "fakeOnly": True,
            "simulatedOutput": True, "publishable": False,
            "capabilitySnapshotHash": snapshot["snapshotHash"],
            "projectRevision": project.revision,
            "quota": snapshot["quota"],
        }

    @created_app.post("/projects/{project_id}/talking-head/fake-generation-tasks", status_code=202)
    def create_fake_talking_head_task(
        project_id: str,
        req: TalkingHeadFakeGenerationRequest,
        idempotency_key_header: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=8, max_length=128
        ),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        begin_serialized_write(db)
        project = project_for_tenant(db, project_id, context)
        # Protect even the existing-idempotency early return. A queued task
        # must not be returned or reserved for an actor the Worker cannot claim.
        require_fake_talking_head_claim_authority(db, project, context)
        key = str(req.idempotencyKey)
        if idempotency_key_header is not None and idempotency_key_header != key:
            raise HTTPException(
                status_code=409,
                detail={"code": "IDEMPOTENCY_KEY_MISMATCH", "message": "请求体与请求头幂等键不一致"},
            )
        user_payload = req.model_dump(mode="json")
        payload_hash = generation_request_hash(user_payload)
        existing = db.execute(select(DBGenerationTask).where(
            DBGenerationTask.tenant_id == context.tenant_id,
            DBGenerationTask.idempotency_key == key,
        )).scalar_one_or_none()
        if existing is not None:
            if (
                existing.project_id != project_id
                or existing.provider != "deterministic-fake-talking-head"
                or existing.requested_by != context.user_id
                or existing.request_hash != payload_hash
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "IDEMPOTENCY_KEY_REUSED", "message": "幂等键已用于其他任务"},
                )
            return generation_task_response(
                db, existing, rights=generation_rights(db, existing), include_history=True
            )
        snapshot, asset = validate_fake_talking_head_request(db, project, req, context)
        quota = snapshot["quota"]
        if (
            int(quota["concurrentRemaining"]) < 1
            or int(quota["monthlyRequestRemaining"]) < 1
            or int(quota["monthlyGeneratedSecondsRemaining"]) < req.durationSeconds
        ):
            raise HTTPException(
                status_code=429,
                detail={"code": "FAKE_TALKING_HEAD_QUOTA_EXCEEDED", "message": "测试生成额度不足"},
            )
        if req.durationSeconds > int(snapshot["clipDurationSeconds"]["max"]) or int(snapshot["maxOutputs"]) != 1:
            raise HTTPException(
                status_code=422,
                detail={"code": "FAKE_TALKING_HEAD_CAPABILITY_MISMATCH", "message": "合成输出参数与测试能力不一致"},
            )
        now = utc_now()
        task_id = str(uuid.uuid4())
        # Actor/tenant/project are always bound to the authenticated API session.
        # Only the nonproduction Worker can read this internal typed FAKE marker.
        payload = {
            **user_payload,
            "fakeTaskType": "talking-head",
            "videoSubject": req.text,
            "videoAspect": "9:16",
            "videoClipDuration": req.durationSeconds,
            "outputCount": 1,
            "inputAssetVersionIds": [asset.id],
            "imageAssetId": asset.id,
            "sourceAssetSha256": asset.sha256,
            "voiceId": "synthetic-seed",
            "simulatedOutput": True,
            "publishable": False,
        }
        task = DBGenerationTask(
            id=task_id, tenant_id=context.tenant_id, project_id=project.id,
            requested_by=context.user_id, provider="deterministic-fake-talking-head",
            status="QUEUED", progress=0, message="已排队（FAKE-ONLY模拟口播，非真实合成）",
            request_json=payload, request_hash=payload_hash,
            capability_snapshot_json=snapshot, capability_snapshot_hash=snapshot["snapshotHash"],
            idempotency_key=key, attempts=1, max_attempts=1,
            created_at=now, updated_at=now,
        )
        attempt = DBGenerationAttempt(
            id=str(uuid.uuid4()), generation_task_id=task_id, attempt_no=1,
            status="QUEUED", reconciliation_state="NOT_SUBMITTED", created_at=now,
        )
        # PostgreSQL enforces the attempt -> task FK immediately; persist the
        # parent row before adding the first attempt (SQLite previously masked
        # this ordering defect in the FAKE-only integration suite).
        db.add(task)
        db.flush()
        db.add(attempt)
        add_usage_entry(
            db, task=task, attempt=attempt, kind="RESERVED",
            request_units=1, generated_seconds=0,
            reservation_key=f"generation:{task.id}",
            config_version_id=snapshot.get("configVersionId"), now=now,
        )
        add_generation_event(
            db, task, attempt=attempt, event_type="TASK_CREATED",
            actor_type="USER", actor_id=context.user_id, to_status="QUEUED",
            metadata={
                "fakeOnly": True, "simulatedOutput": True, "actorRole": context.role,
                "typedRequestHash": payload_hash,
                "inputAssetVersionId": asset.id, "sourceAssetSha256": asset.sha256,
                "usageReserved": True,
            }, now=now,
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            concurrent = db.execute(select(DBGenerationTask).where(
                DBGenerationTask.tenant_id == context.tenant_id,
                DBGenerationTask.idempotency_key == key,
            )).scalar_one_or_none()
            if (
                concurrent is not None
                and concurrent.project_id == project_id
                and concurrent.provider == "deterministic-fake-talking-head"
                and concurrent.requested_by == context.user_id
                and concurrent.request_hash == payload_hash
            ):
                return generation_task_response(
                    db, concurrent, rights=generation_rights(db, concurrent), include_history=True
                )
            raise HTTPException(
                status_code=409,
                detail={"code": "FAKE_TALKING_HEAD_CREATE_CONFLICT", "message": "合成测试任务发生并发冲突"},
            ) from exc
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )

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
        quota = snapshot["quota"]
        if (
            snapshot.get("circuit", {}).get("state") == "HALF_OPEN"
            and int(quota["concurrentRemaining"]) < int(quota["concurrentLimit"])
        ):
            raise HTTPException(
                status_code=503,
                detail={"code": "GENERATION_HALF_OPEN_PROBE_BUSY", "message": "熔断半开探测任务正在执行"},
            )
        if int(quota["concurrentRemaining"]) < 1:
            raise HTTPException(
                status_code=429,
                detail={"code": "GENERATION_CONCURRENCY_QUOTA_EXCEEDED", "message": "并发生成任务已达到上限"},
            )
        if int(quota["monthlyRequestRemaining"]) < 1:
            raise HTTPException(
                status_code=429,
                detail={"code": "GENERATION_MONTHLY_REQUEST_QUOTA_EXCEEDED", "message": "月度生成请求已达到上限"},
            )
        if int(quota["monthlyGeneratedSecondsRemaining"]) < 1:
            raise HTTPException(
                status_code=429,
                detail={"code": "GENERATION_MONTHLY_SECONDS_QUOTA_EXCEEDED", "message": "月度生成时长已达到上限"},
            )
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
        # PostgreSQL enforces the attempt -> task FK immediately; persist the
        # parent row before adding the first attempt (SQLite previously masked
        # this ordering defect in the FAKE-only integration suite).
        db.add(task)
        db.flush()
        db.add(attempt)
        add_usage_entry(
            db, task=task, attempt=attempt, kind="RESERVED",
            request_units=1, generated_seconds=0,
            reservation_key=f"generation:{task.id}",
            config_version_id=snapshot.get("configVersionId"), now=now,
        )
        add_generation_event(
            db, task, attempt=attempt, event_type="TASK_CREATED",
            actor_type="USER", actor_id=context.user_id, to_status="QUEUED",
            metadata={"usageReserved": True, "requestUnits": 1}, now=now,
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
        pagination: str = Query(default="offset", min_length=6, max_length=8),
        after: str | None = Query(default=None, max_length=2048),
        status: str | None = Query(default=None, min_length=1, max_length=32),
        provider: str | None = Query(default=None, min_length=1, max_length=80),
        requestedBy: str | None = Query(default=None, min_length=1, max_length=128),
        createdAfter: datetime.datetime | None = Query(default=None),
        createdBefore: datetime.datetime | None = Query(default=None),
        errorCode: str | None = Query(default=None, min_length=1, max_length=120),
        sort: str = Query(default="createdAt:desc", min_length=1, max_length=32),
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project = project_for_tenant(db, project_id, context)
        # G72 FAKE-only read fence: tenant membership is not a project grant.
        # Apply it before pagination; generic provider tasks are unaffected.
        can_read_fake_tasks = (
            context.role == "owner" and project.owner_id == context.user_id
            and db.execute(select(DBUser.id).where(
                DBUser.id == context.user_id,
                DBUser.tenant_id == context.tenant_id,
                DBUser.role == "owner",
                DBUser.is_active.is_(True),
            )).scalar_one_or_none() is not None
        )
        if pagination not in {"offset", "keyset"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_PAGINATION_UNSUPPORTED", "message": "不支持的任务分页模式"},
            )
        if sort not in GENERATION_TASK_LIST_SORTS:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_SORT_UNSUPPORTED", "message": "任务排序仅支持创建时间升序或降序"},
            )
        if status is not None and status not in GENERATION_TASK_LIST_STATUSES:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_STATUS_FILTER_INVALID", "message": "任务状态筛选值无效"},
            )
        provider = provider.strip() if provider is not None else None
        requestedBy = requestedBy.strip() if requestedBy is not None else None
        errorCode = errorCode.strip() if errorCode is not None else None
        if provider == "" or requestedBy == "" or errorCode == "":
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_FILTER_INVALID", "message": "任务筛选条件不能为空字符串"},
            )
        if createdAfter is not None and (
            createdAfter.tzinfo is None or createdAfter.utcoffset() is None
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_TIME_FILTER_TIMEZONE_REQUIRED", "message": "创建时间筛选必须包含时区"},
            )
        if createdBefore is not None and (
            createdBefore.tzinfo is None or createdBefore.utcoffset() is None
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_TIME_FILTER_TIMEZONE_REQUIRED", "message": "创建时间筛选必须包含时区"},
            )
        created_after_utc = createdAfter.astimezone(datetime.timezone.utc) if createdAfter else None
        created_before_utc = createdBefore.astimezone(datetime.timezone.utc) if createdBefore else None
        if (
            created_after_utc is not None
            and created_before_utc is not None
            and created_after_utc > created_before_utc
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_TIME_RANGE_INVALID", "message": "创建时间起点不能晚于终点"},
            )
        if pagination == "keyset" and cursor != 0:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_CURSOR_MODE_MISMATCH", "message": "稳定游标模式不能同时使用旧偏移量"},
            )
        if pagination == "offset" and after is not None:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_CURSOR_MODE_MISMATCH", "message": "偏移量模式不能使用稳定游标"},
            )

        query = select(DBGenerationTask).where(
            DBGenerationTask.project_id == project_id,
            DBGenerationTask.tenant_id == context.tenant_id,
        )
        if can_read_fake_tasks:
            # Project ownership alone must not disclose historical FAKE jobs
            # requested by the previous Owner after an ownership transfer.
            query = query.where(or_(
                DBGenerationTask.provider != "deterministic-fake-talking-head",
                DBGenerationTask.requested_by == context.user_id,
            ))
        else:
            query = query.where(
                DBGenerationTask.provider != "deterministic-fake-talking-head"
            )
        if status is not None:
            # The status filter is explicitly against the persisted state, not the
            # rights-derived display status returned in the response.
            query = query.where(DBGenerationTask.status == status)
        if provider is not None:
            query = query.where(DBGenerationTask.provider == provider)
        if requestedBy is not None:
            query = query.where(DBGenerationTask.requested_by == requestedBy)
        if created_after_utc is not None:
            query = query.where(DBGenerationTask.created_at >= created_after_utc)
        if created_before_utc is not None:
            query = query.where(DBGenerationTask.created_at <= created_before_utc)
        if errorCode is not None:
            query = query.where(DBGenerationTask.error_code == errorCode)

        scope_hash = generation_task_list_scope_hash(
            tenant_id=context.tenant_id,
            project_id=project_id,
            status=status,
            provider=provider,
            requested_by=requestedBy,
            created_after=created_after_utc,
            created_before=created_before_utc,
            error_code=errorCode,
            sort=sort,
        )
        cursor_time: datetime.datetime | None = None
        cursor_task_id: str | None = None
        if pagination == "keyset" and after is not None:
            cursor_time, cursor_task_id = decode_generation_task_cursor(
                after, expected_scope_hash=scope_hash
            )
            if sort == "createdAt:desc":
                query = query.where(or_(
                    DBGenerationTask.created_at < cursor_time,
                    and_(
                        DBGenerationTask.created_at == cursor_time,
                        DBGenerationTask.id < cursor_task_id,
                    ),
                ))
            else:
                query = query.where(or_(
                    DBGenerationTask.created_at > cursor_time,
                    and_(
                        DBGenerationTask.created_at == cursor_time,
                        DBGenerationTask.id > cursor_task_id,
                    ),
                ))

        if sort == "createdAt:desc":
            query = query.order_by(DBGenerationTask.created_at.desc(), DBGenerationTask.id.desc())
        else:
            query = query.order_by(DBGenerationTask.created_at.asc(), DBGenerationTask.id.asc())
        if pagination == "offset":
            query = query.offset(cursor)
        rows = list(db.execute(query.limit(pageSize + 1)).scalars())
        has_more = len(rows) > pageSize
        items = rows[:pageSize]
        next_keyset_cursor = (
            encode_generation_task_cursor(items[-1], scope_hash)
            if pagination == "keyset" and has_more and items
            else None
        )
        return {
            "items": [
                generation_task_response(db, task, rights=generation_rights(db, task))
                for task in items
            ],
            # Preserve the legacy integer cursor contract for existing clients.
            "nextCursor": cursor + pageSize if pagination == "offset" and has_more else None,
            "nextKeysetCursor": next_keyset_cursor,
            "pagination": pagination,
            "sort": sort,
            "sortTieBreaker": "taskId",
            "statusFilterSemantics": "storedStatus",
        }

    @created_app.get("/projects/{project_id}/generation-tasks/{task_id}")
    def get_generation_task(
        project_id: str,
        task_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        project = project_for_tenant(db, project_id, context)
        task = generation_task_for_tenant(db, project_id, task_id, context)
        if task.provider == "deterministic-fake-talking-head":
            # An unassigned Editor/Admin/Owner cannot see the synthetic script
            # or history; no delegated project-read policy is inferred here.
            require_fake_talking_head_claim_authority(db, project, context)
            if task.requested_by != context.user_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权读取此模拟口播任务",
                    },
                )
        return generation_task_response(
            db, task, rights=generation_rights(db, task), include_history=True
        )



    @created_app.post("/projects/{project_id}/detection-evidence")
    def produce_detection_evidence(
        project_id: str,
        req: DetectionEvidenceRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        """Produce machine evidence for human review; never creates an approval."""
        require_roles(context, "owner", "editor")
        project = project_for_tenant(db, project_id, context)
        if project.revision != req.timelineVersion:
            raise HTTPException(
                status_code=409,
                detail={"code": "CONCURRENCY_CONFLICT", "message": "检测证据必须绑定当前项目版本"},
            )
        rights = normalize_rights_evidence(
            media_ids=req.mediaIds,
            rights=req.rightsEvidence,
        )
        return build_release_evidence(
            timeline_version=project.revision,
            timeline=project.timeline,
            preview_evidence_ref=req.previewEvidenceRef,
            final_evidence_ref=req.finalEvidenceRef,
            media_probe=req.mediaProbe,
            talking_head_metrics=req.talkingHeadMetrics,
            rights_evidence=rights,
            rule_pack=req.rulePack,
            rule_content=req.ruleContent,
            source_digest=req.sourceDigest,
            caption_digest=req.captionDigest,
            audio_digest=req.audioDigest,
            render_parity=req.renderParity,
        )

    @created_app.post(
        "/projects/{project_id}/generation-tasks/{task_id}/apply-talking-head-draft",
        response_model=ProjectResponse,
    )
    def apply_talking_head_draft(
        project_id: str,
        task_id: str,
        req: ApplyTalkingHeadDraftRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        """Explicitly apply one rights-cleared generation result to the project draft timeline."""
        require_roles(context, "owner", "editor")
        project = project_for_tenant(db, project_id, context)
        if project.revision != req.expectedRevision:
            raise HTTPException(
                status_code=409,
                detail={"code": "CONCURRENCY_CONFLICT", "message": "项目版本冲突，请重新载入后再应用口播草稿"},
            )
        task = generation_task_for_tenant(db, project_id, task_id, context)
        # G72 FAKE-only: publishing a generated task into a draft is a project
        # mutation, not a tenant-wide Editor privilege. Align with the
        # existing Worker claim and task read/cancel originator fences.
        if task.provider == "deterministic-fake-talking-head":
            require_fake_talking_head_claim_authority(db, project, context)
            if task.requested_by != context.user_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权应用此模拟口播任务",
                    },
                )
        task_rights = generation_rights(db, task)
        if not task_rights.get("allowed"):
            raise HTTPException(
                status_code=422,
                detail={"code": task_rights.get("code", "RIGHTS_MISSING"), "message": "生成结果权利检查未通过"},
            )
        if not task.asset_version_id:
            raise HTTPException(
                status_code=422,
                detail={"code": "GENERATION_RESULT_MISSING", "message": "生成任务尚无可应用结果"},
            )
        video_asset = db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.id == task.asset_version_id,
                DBAssetVersion.project_id == project_id,
                DBAssetVersion.tenant_id == context.tenant_id,
            )
        ).scalar_one_or_none()
        if video_asset is None or video_asset.media_type != "video":
            raise HTTPException(
                status_code=422,
                detail={"code": "TALKING_HEAD_VIDEO_REQUIRED", "message": "口播草稿需要视频生成结果"},
            )
        probe = video_asset.probe_json or {}
        duration_seconds = float(probe.get("durationSeconds") or 0)
        if duration_seconds <= 0:
            raise HTTPException(
                status_code=422,
                detail={"code": "TALKING_HEAD_DURATION_REQUIRED", "message": "生成结果缺少有效时长"},
            )

        audio_media_id = None
        if req.audioAssetVersionId:
            audio_asset = db.execute(
                select(DBAssetVersion).where(
                    DBAssetVersion.id == req.audioAssetVersionId,
                    DBAssetVersion.project_id == project_id,
                    DBAssetVersion.tenant_id == context.tenant_id,
                )
            ).scalar_one_or_none()
            if audio_asset is None or audio_asset.media_type != "audio":
                raise HTTPException(
                    status_code=422,
                    detail={"code": "TALKING_HEAD_AUDIO_INVALID", "message": "独立旁白素材无效"},
                )
            audio_rights = generated_asset_export_rights(db, audio_asset)
            if audio_rights is None:
                audio_rights = rights_decision(
                    latest_rights_snapshot(db, audio_asset.id, "EXPORT"),
                    utc_now(),
                )
            if not audio_rights.get("allowed"):
                raise HTTPException(
                    status_code=422,
                    detail={"code": audio_rights.get("code", "RIGHTS_MISSING"), "message": "独立旁白权利检查未通过"},
                )
            audio_media_id = audio_asset.media_id

        request_json = task.request_json or {}
        requested_aspect = str(request_json.get("videoAspect") or req.aspect)
        if requested_aspect != req.aspect:
            raise HTTPException(
                status_code=422,
                detail={"code": "TALKING_HEAD_ASPECT_MISMATCH", "message": "口播草稿比例与生成请求不一致"},
            )
        timeline = build_talking_head_timeline(
            video_material_id=video_asset.media_id,
            audio_material_id=audio_media_id,
            duration_ms=max(1, round(duration_seconds * 1000)),
            aspect=req.aspect,
            subtitles=tuple(
                SubtitleCue(cue.text, cue.startMs, cue.durationMs)
                for cue in req.subtitles
            ),
        )
        if created_app.state.governance_gates:
            enforce_timeline_receipt(
                req.timelineReceipt,
                current_version=project.revision,
            )
        updated = apply_project_update(
            db,
            project_id,
            UpdateProjectRequest(timeline=timeline, expectedRevision=req.expectedRevision),
            tenant_id=context.tenant_id,
        )
        return project_response(updated)

    @created_app.post("/projects/{project_id}/generation-tasks/{task_id}/cancel")
    def cancel_generation_task(
        project_id: str,
        task_id: str,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        require_roles(context, "owner", "editor")
        task = generation_task_for_tenant(
            db, project_id, task_id, context, for_update=True
        )
        # G72 FAKE-only safety: a tenant-wide Editor cannot cancel another
        # Owner's queued/leased synthetic task and release its reservation.
        # Reuse the existing Worker/creation Owner contract; do not infer
        # project grants or delegated-admin privileges from tenant membership.
        # Historical tasks with a mismatched originator remain isolated
        # pending the Owner-approved recovery/settlement policy.
        if task.provider == "deterministic-fake-talking-head":
            project = project_for_tenant(db, project_id, context)
            require_fake_talking_head_claim_authority(db, project, context)
            if task.requested_by != context.user_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权取消此模拟口播任务",
                    },
                )
        if task.cancel_requested_at is None and task.status not in TERMINAL_GENERATION_STATES:
            now = utc_now()
            previous = task.status
            attempt = current_generation_attempt(db, task)
            # A QUEUED current attempt does not prove older attempts were
            # never submitted. Legacy multi-attempt rows can retain a HELD
            # or accepted upstream result after task-level IDs were cleared.
            # Never release this task's RESERVED usage without an independent
            # reconciliation/STOP ACK for every prior submission.
            unresolved_prior = db.execute(
                select(DBGenerationAttempt.id).where(
                    DBGenerationAttempt.generation_task_id == task.id,
                    DBGenerationAttempt.attempt_no < task.attempts,
                    or_(
                        DBGenerationAttempt.reconciliation_state != "NOT_SUBMITTED",
                        DBGenerationAttempt.submission_started_at.is_not(None),
                        DBGenerationAttempt.upstream_job_id.is_not(None),
                        DBGenerationAttempt.status != "FAILED",
                    ),
                ).limit(1)
            ).scalar_one_or_none()
            # A missing or renumbered older attempt is not a trusted STOP ACK.
            # Preserve RESERVED usage until the full FAILED/NOT_SUBMITTED
            # attempt history is present and independently reconcilable.
            # A count alone can hide migrated attempt 0 while attempt 1 is missing.
            # Require exact positive prior numbering before quota release or retry.
            earlier_attempt_numbers = db.execute(
                select(DBGenerationAttempt.attempt_no).where(
                    DBGenerationAttempt.generation_task_id == task.id,
                    DBGenerationAttempt.attempt_no < task.attempts,
                ).order_by(DBGenerationAttempt.attempt_no)
            ).scalars().all()
            earlier_attempt_sequence_valid = (
                len(earlier_attempt_numbers) == task.attempts - 1
                and all(
                    number == expected
                    for expected, number in enumerate(earlier_attempt_numbers, start=1)
                )
            )
            # A legacy attempt numbered beyond task.attempts may still contain
            # an accepted/billable upstream result. The current row being clean
            # does not authorize quota release when history extends into future.
            unexpected_future_attempt = db.execute(
                select(DBGenerationAttempt.id).where(
                    DBGenerationAttempt.generation_task_id == task.id,
                    DBGenerationAttempt.attempt_no > task.attempts,
                ).limit(1)
            ).scalar_one_or_none()
            # G70/G71 + M11 FAKE-only fail-closed prerequisite. A claimed
            # submission can still be executing or billable upstream even
            # when the API has no upstream job ID. Without an independently
            # trusted STOP ACK, neither record CANCELED nor RELEASE quota.
            # This candidate intentionally does not invent settlement or
            # stop/ACK policy; it keeps the original task for reconciliation.
            if (
                task.status != "QUEUED"
                or attempt.status != "QUEUED"
                or attempt.submission_started_at is not None
                # Durable HELD/accepted evidence is not a preclaim cancellation.
                or attempt.reconciliation_state != "NOT_SUBMITTED"
                or unresolved_prior is not None
                or task.attempts < 1
                or not earlier_attempt_sequence_valid
                or unexpected_future_attempt is not None
                # Orphaned lease markers imply possible Worker ownership even
                # when historical status fields still read QUEUED.
                or task.lease_owner is not None
                or task.lease_expires_at is not None
                or task.upstream_job_id is not None
                or attempt.upstream_job_id is not None
                or task.provider_artifact_id is not None
                or task.media_id is not None
                or task.asset_version_id is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "GENERATION_CANCEL_RECONCILIATION_REQUIRED",
                        "message": "任务可能已被上游受理，缺少可信停止回执，禁止释放预留用量；须人工对账",
                    },
                )
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
            release_generation_reservation(
                db, task, attempt, reason_code="TASK_CANCELED", now=now
            )
            task_circuit = generation_circuit(db, task.tenant_id, for_update=True)
            if (
                task_circuit is not None
                and task_circuit.state == "HALF_OPEN"
                and task_circuit.half_open_task_id == task.id
            ):
                task_circuit.half_open_task_id = None
                task_circuit.half_open_lease_epoch = None
                task_circuit.half_open_issuer = None
                task_circuit.updated_at = now
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
        task = generation_task_for_tenant(
            db, project_id, task_id, context, for_update=True
        )
        # G72 FAKE-only: the shared retry endpoint must not disclose a
        # synthetic Owner's task state or permit a forged future attempt.
        # Reuse existing Worker claim authority; do not infer any new grant.
        if task.provider == "deterministic-fake-talking-head":
            project = project_for_tenant(db, project_id, context)
            require_fake_talking_head_claim_authority(db, project, context)
            if task.requested_by != context.user_id:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "FAKE_TALKING_HEAD_ORIGINATOR_NOT_PROJECT_OWNER",
                        "message": "当前身份无权重试此模拟口播任务",
                    },
                )
        # G70/G71: a completed upstream job may already have produced billable output.
        # A failed media transfer requires reconciliation of the ORIGINAL artifact,
        # not another generation attempt with a new upstream job and possible charge.
        if task.error_code == "ARTIFACT_INGEST_FAILED":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_REQUIRED",
                    "message": "生成结果转存状态未确认，必须先核对原始产物，禁止重新生成",
                },
            )
        if task.status not in RETRYABLE_GENERATION_STATES or (task.error_code or "").startswith("NON_RETRYABLE_"):
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_NOT_RETRYABLE", "message": "当前任务状态不可重试"},
            )
        # G70/G71 FAKE-only safety prerequisite: a retryable task status is not
        # evidence that the prior submission was never accepted or charged.
        # Until an independently trusted human ACK/HELD reconciliation is
        # approved, only attempts durably marked NOT_SUBMITTED with no upstream
        # receipt on either the task or attempt may enter the existing retry.
        # Refuse malformed legacy/divergent history without changing usage.
        # A clean CURRENT attempt cannot prove that earlier attempts were
        # never accepted. Legacy retries may have cleared task-level upstream
        # IDs even while an older attempt retains a durable HELD, submission
        # start or upstream job marker. Do not create a third billable attempt
        # without independent reconciliation of the entire attempt history.
        unresolved_earlier_attempt = db.execute(
            select(DBGenerationAttempt.id).where(
                DBGenerationAttempt.generation_task_id == task.id,
                DBGenerationAttempt.attempt_no < task.attempts,
                or_(
                    DBGenerationAttempt.reconciliation_state != "NOT_SUBMITTED",
                    DBGenerationAttempt.submission_started_at.is_not(None),
                    DBGenerationAttempt.upstream_job_id.is_not(None),
                    DBGenerationAttempt.status != "FAILED",
                ),
            ).limit(1)
        ).scalar_one_or_none()
        # Earlier attempt rows must form a complete, terminal FAILED prefix.
        # A missing/corrupted row cannot prove that a billable POST never ran.
        # A count alone can hide migrated attempt 0 while attempt 1 is missing.
        # Require exact positive prior numbering before quota release or retry.
        earlier_attempt_numbers = db.execute(
            select(DBGenerationAttempt.attempt_no).where(
                DBGenerationAttempt.generation_task_id == task.id,
                DBGenerationAttempt.attempt_no < task.attempts,
            ).order_by(DBGenerationAttempt.attempt_no)
        ).scalars().all()
        earlier_attempt_sequence_valid = (
            len(earlier_attempt_numbers) == task.attempts - 1
            and all(
                number == expected
                for expected, number in enumerate(earlier_attempt_numbers, start=1)
            )
        )
        # A migrated future attempt can retain a submitted/billable result
        # even when the task's current attempt says NOT_SUBMITTED.
        # Reject before mutating attempts, use, lease, or audit.
        unexpected_future_attempt = db.execute(
            select(DBGenerationAttempt.id).where(
                DBGenerationAttempt.generation_task_id == task.id,
                DBGenerationAttempt.attempt_no > task.attempts,
            ).limit(1)
        ).scalar_one_or_none()
        prior_attempt = db.execute(
            select(DBGenerationAttempt).where(
                DBGenerationAttempt.generation_task_id == task.id,
                DBGenerationAttempt.attempt_no == task.attempts,
            )
        ).scalar_one_or_none()
        if (
            prior_attempt is None
            or unresolved_earlier_attempt is not None
            or task.attempts < 1
            or not earlier_attempt_sequence_valid
            or unexpected_future_attempt is not None
            or prior_attempt.status != task.status
            or prior_attempt.reconciliation_state != "NOT_SUBMITTED"
            # Worker claim durably writes this BEFORE any upstream POST. The
            # missing receipt is ambiguous after a crash/timeout, not proof
            # that no billable request was accepted; never replay blindly.
            or prior_attempt.submission_started_at is not None
            # G70/G71: a legacy FAILED status does not revoke an orphaned
            # Worker lease. Even an expired/missing counterpart lease marker
            # means the previous dispatch might still be executing upstream.
            # Do not erase the lease or create a new potentially billable attempt
            # without independently trusted stop/reconciliation evidence.
            or task.lease_owner is not None
            or task.lease_expires_at is not None
            or task.upstream_job_id is not None
            or task.provider_artifact_id is not None
            or task.media_id is not None
            or task.asset_version_id is not None
            or prior_attempt.upstream_job_id is not None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_RECONCILIATION_REQUIRED",
                    "message": "原尝试存在已提交或状态不明的持久证据，禁止自动重试，须人工对账",
                },
            )
        if task.attempts >= task.max_attempts:
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_RETRY_EXHAUSTED", "message": "生成任务已达到最大尝试次数"},
            )
        readiness = current_generation_capabilities(db, context.tenant_id)
        if not readiness["enabled"]:
            raise HTTPException(
                status_code=503,
                detail={"code": readiness["reasonCode"], "message": "生成服务当前不可重试"},
            )
        if (
            readiness.get("configVersionId") != task.capability_snapshot_json.get("configVersionId")
            or readiness.get("policyHash") != task.capability_snapshot_json.get("policyHash")
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "GENERATION_CONFIG_SUPERSEDED", "message": "任务配置已被替代，请新建生成请求"},
            )
        if int(readiness["quota"]["concurrentRemaining"]) < 1:
            raise HTTPException(
                status_code=429,
                detail={"code": "GENERATION_CONCURRENCY_QUOTA_EXCEEDED", "message": "并发生成任务已达到上限"},
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

    @created_app.post(
        "/projects/{project_id}/generation-tasks/{task_id}/recover-artifact"
    )
    def recover_generation_artifact(
        project_id: str,
        task_id: str,
        req: RecoverGenerationArtifactRequest,
        context: AuthContext = Depends(context_dependency),
        db: Session = Depends(db_dependency),
    ):
        """Owner-only recovery of a verified orphan already present in video-use.

        This route never calls a generation Provider and never uploads media.
        If the original object is absent, upstream retrieval remains a separate,
        explicitly gated integration and this route fails closed.
        """
        require_roles(context, "owner")
        project = project_for_tenant(db, project_id, context)
        task = generation_task_for_tenant(db, project_id, task_id, context)
        # G72 FAKE-only: original ownership alone is insufficient after a
        # project transfer. Do not inspect/reconcile an old Owner's synthetic
        # artifact or expose its history without current Worker claim authority.
        if task.provider == "deterministic-fake-talking-head":
            require_fake_talking_head_claim_authority(db, project, context)
        if context.user_id != task.requested_by:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "OWNER_SELF_ONLY",
                    "message": "仅原任务发起Owner本人可以恢复该任务的原始产物",
                },
            )

        if task.asset_version_id:
            return generation_task_response(
                db, task, rights=generation_rights(db, task), include_history=True
            )
        if task.status != "FAILED" or task.error_code != "ARTIFACT_INGEST_FAILED":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_NOT_REQUIRED",
                    "message": "当前任务不处于原始产物转存待恢复状态",
                },
            )

        attempt = current_generation_attempt(db, task)
        if attempt is None or attempt.reconciliation_state != "HELD":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_EVIDENCE_MISSING",
                    "message": "缺少 HELD 对账状态，禁止人工恢复",
                },
            )
        if not task.upstream_job_id or not task.provider_artifact_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORIGINAL_ARTIFACT_IDENTITY_MISSING",
                    "message": "原始上游任务或产物编号不完整，禁止猜测恢复",
                },
            )

        failure_event = db.execute(
            select(DBGenerationEvent)
            .where(
                DBGenerationEvent.generation_task_id == task.id,
                DBGenerationEvent.attempt_id == attempt.id,
                DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
            )
            .order_by(DBGenerationEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        evidence = (
            failure_event.metadata_json
            if failure_event is not None and isinstance(failure_event.metadata_json, dict)
            else {}
        )
        orphan_media_id = str(evidence.get("orphanedMediaId") or "")
        if not orphan_media_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED",
                    "message": "Sidecar 中没有已登记的孤儿媒资；本操作不会调用 Provider，需另行审批原始上游产物读取。",
                },
            )
        if (
            task.media_id != orphan_media_id
            or evidence.get("originalUpstreamJobId") != task.upstream_job_id
            or evidence.get("originalProviderArtifactId") != task.provider_artifact_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_EVIDENCE_MISMATCH",
                    "message": "任务、上游产物与孤儿媒资证据不一致",
                },
            )

        expected_sha256 = str(evidence.get("orphanedMediaSha256") or "").lower()
        # Failed-attempt audit evidence must be an exact byte count. Booleans,
        # floats and numeric strings are not an independent storage receipt.
        raw_expected_size = evidence.get("orphanedMediaSizeBytes")
        expected_size = raw_expected_size if type(raw_expected_size) is int else 0
        if (
            len(expected_sha256) != 64
            or any(char not in "0123456789abcdef" for char in expected_sha256)
            or expected_size <= 0
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_EVIDENCE_INVALID",
                    "message": "孤儿媒资缺少有效 SHA256 或字节数证据",
                },
            )

        try:
            inspected = created_app.state.video_use.inspect_media(
                project_id, orphan_media_id
            )
        except VideoUseNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORIGINAL_ARTIFACT_RETRIEVAL_REQUIRED",
                    "message": "Sidecar 中未找到原始孤儿媒资；未执行上传或 Provider 调用",
                },
            ) from exc
        except VideoUseError as exc:
            logger.exception("video-use orphan media inspection failed")
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "ARTIFACT_RECOVERY_INSPECTION_UNAVAILABLE",
                    "message": "无法读取孤儿媒资校验信息，任务继续保持 HELD",
                },
            ) from exc

        # G70/G71: a malformed Sidecar inspection JSON root is not proof
        # that an orphan is recoverable. Preserve HELD/usage unchanged and
        # return a stable upstream-inspection failure instead of HTTP 500.
        if not isinstance(inspected, dict):
            raise HTTPException(
                status_code=502,
                detail={
                    "code": "ARTIFACT_RECOVERY_INSPECTION_UNAVAILABLE",
                    "message": "孤儿媒资校验响应格式异常，任务继续保持 HELD",
                },
            )
        actual_sha256 = str(inspected.get("sha256") or "").lower()
        # The Sidecar JSON is untrusted. Never coerce a bool, string or float
        # to an integer byte count before verifying an Owner-only recovery.
        raw_actual_size = inspected.get("sizeBytes")
        actual_size = raw_actual_size if type(raw_actual_size) is int else 0
        probe_metadata = inspected.get("metadata")
        video_metadata = (
            probe_metadata.get("video")
            if isinstance(probe_metadata, dict)
            else None
        )
        raw_duration = (
            probe_metadata.get("durationSeconds")
            if isinstance(probe_metadata, dict) else None
        )
        try:
            duration_seconds = (
                float(raw_duration) if type(raw_duration) in (int, float) else 0
            )
        except (TypeError, ValueError, OverflowError):
            duration_seconds = 0
        # Historical snapshots can contain non-object providerPolicy JSON.
        # Owner recovery must refuse malformed policy evidence before changing
        # any media, quota or settlement state (no Provider call).
        raw_snapshot = task.capability_snapshot_json
        if not isinstance(raw_snapshot, dict):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_POLICY_INVALID",
                    "message": "原任务生成快照证据格式异常，保持 HELD，需人工核对",
                },
            )
        raw_provider_policy = raw_snapshot.get("providerPolicy")
        if raw_provider_policy is not None and not isinstance(raw_provider_policy, dict):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_POLICY_INVALID",
                    "message": "原任务生成策略证据格式异常，保持 HELD，需人工核对",
                },
            )
        provider_policy = raw_provider_policy or {}
        # Missing historical limit retains the existing global upload cap.
        # A present but malformed stored limit is NOT evidence of a larger
        # allowance. Never silently fall back to the global cap in that case.
        if "maxArtifactBytes" not in provider_policy:
            policy_max_bytes = max_upload_bytes
        else:
            raw_limit = provider_policy["maxArtifactBytes"]
            try:
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
                    raise ValueError("maxArtifactBytes is not an integer")
                policy_max_bytes = int(raw_limit)
                if not (1 <= policy_max_bytes <= 2 * 1024**3):
                    raise ValueError("maxArtifactBytes exceeds schema bounds")
            except (TypeError, ValueError, OverflowError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_RECOVERY_POLICY_INVALID",
                        "message": "原任务产物大小策略证据异常，保持 HELD，需人工核对",
                    },
                ) from exc

        if (
            inspected.get("projectId") != project_id
            or inspected.get("mediaId") != orphan_media_id
            or inspected.get("contentType") != "video/mp4"
            or not hmac.compare_digest(actual_sha256, expected_sha256)
            or actual_size != expected_size
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORPHAN_MEDIA_INTEGRITY_MISMATCH",
                    "message": "孤儿媒资项目、ID、SHA256、字节数或类型与失败事件不一致",
                },
            )
        if (
            actual_size <= 0
            or actual_size > min(max_upload_bytes, policy_max_bytes)
        ):
            raise HTTPException(
                status_code=413,
                detail={
                    "code": "ORPHAN_MEDIA_SIZE_OUT_OF_POLICY",
                    "message": "孤儿媒资大小超出原任务允许范围",
                },
            )
        # Sidecar probe data is untrusted. A malformed numeric dimension must
        # fail closed with a stable 422, not raise ValueError/TypeError as 500
        # during Owner-only orphan recovery (while usage remains HELD).
        # Media dimensions are measured integers. Coercing a boolean or
        # numeric string would convert invalid probe evidence into an approval.
        raw_width = video_metadata.get("width") if isinstance(video_metadata, dict) else None
        raw_height = video_metadata.get("height") if isinstance(video_metadata, dict) else None
        probed_width = raw_width if type(raw_width) is int else 0
        probed_height = raw_height if type(raw_height) is int else 0
        if (
            not isinstance(video_metadata, dict)
            or probed_width <= 0
            or probed_height <= 0
            or not math.isfinite(duration_seconds)
            or not (0 < duration_seconds <= 86_400)
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ORPHAN_MEDIA_PROBE_INVALID",
                    "message": "孤儿媒资未通过重新执行的 FFprobe 视频校验",
                },
            )

        # The sidecar call above is read-only. Re-open a serialized transaction
        # and re-check every mutable binding before quota, asset or settlement writes.
        db.rollback()
        begin_serialized_write(db)
        task = generation_task_for_tenant(
            db, project_id, task_id, context, for_update=True
        )
        if context.user_id != task.requested_by:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "OWNER_SELF_ONLY",
                    "message": "恢复提交时任务发起人已变化，拒绝继续",
                },
            )
        project_statement = select(DBProject).where(
            DBProject.id == project_id,
            DBProject.tenant_id == context.tenant_id,
        )
        if app_engine.dialect.name != "sqlite":
            project_statement = project_statement.with_for_update()
        project = db.execute(project_statement).scalar_one_or_none()
        if project is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "PROJECT_NOT_FOUND", "message": "未找到该项目"},
            )
        # G72 FAKE-only: Sidecar inspection occurs outside the final DB write
        # lock. A project Owner transfer or originator deactivation during that
        # read-only call must not authorize a stale Owner to publish/recover the
        # historical FAKE artifact. Recheck current claim authority inside the
        # serialized final transaction, before any asset or quota changes.
        if task.provider == "deterministic-fake-talking-head":
            require_fake_talking_head_claim_authority(db, project, context)
        # Sidecar inspection runs outside the final write transaction. A
        # concurrent change to the saved generation capability/policy snapshot
        # invalidates the limits checked before the probe. Do not register an
        # orphan using stale limits; keep the original HELD ledger unchanged.
        if task.capability_snapshot_json != raw_snapshot:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_POLICY_CHANGED",
                    "message": "恢复期间原任务能力或费用策略快照变化，保持 HELD，需重新核对",
                },
            )
        if task.asset_version_id:
            return generation_task_response(
                db, task, rights=generation_rights(db, task), include_history=True
            )

        attempt = current_generation_attempt(db, task)
        failure_event = db.execute(
            select(DBGenerationEvent)
            .where(
                DBGenerationEvent.generation_task_id == task.id,
                DBGenerationEvent.attempt_id == (attempt.id if attempt else ""),
                DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
            )
            .order_by(DBGenerationEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        current_evidence = (
            failure_event.metadata_json
            if failure_event is not None and isinstance(failure_event.metadata_json, dict)
            else {}
        )
        if (
            task.status != "FAILED"
            or task.error_code != "ARTIFACT_INGEST_FAILED"
            or attempt is None
            or attempt.reconciliation_state != "HELD"
            or task.media_id != orphan_media_id
            or task.upstream_job_id != current_evidence.get("originalUpstreamJobId")
            or task.provider_artifact_id != current_evidence.get("originalProviderArtifactId")
            or current_evidence.get("orphanedMediaId") != orphan_media_id
            or current_evidence.get("orphanedMediaSha256") != evidence.get("orphanedMediaSha256")
            or current_evidence.get("orphanedMediaSizeBytes") != evidence.get("orphanedMediaSizeBytes")
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ARTIFACT_RECOVERY_STATE_CHANGED",
                    "message": "恢复期间任务或原始证据发生变化，请重新核对",
                },
            )

        reserved_entry = db.execute(
            select(DBGenerationUsageEntry).where(
                DBGenerationUsageEntry.task_id == task.id,
                DBGenerationUsageEntry.kind == "RESERVED",
            )
        ).scalar_one_or_none()
        released_entry = db.execute(
            select(DBGenerationUsageEntry.id).where(
                DBGenerationUsageEntry.task_id == task.id,
                DBGenerationUsageEntry.kind == "RELEASED",
            )
        ).scalar_one_or_none()
        existing_settled_entry = db.execute(
            select(DBGenerationUsageEntry.id).where(
                DBGenerationUsageEntry.task_id == task.id,
                DBGenerationUsageEntry.kind == "SETTLED",
            )
        ).scalar_one_or_none()
        if reserved_entry is None or released_entry is not None or existing_settled_entry is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_USAGE_RESERVATION_NOT_RECOVERABLE",
                    "message": "原始用量预留不完整、已释放或已有结算记录；需先由 M11/M13 对账",
                },
            )

        # Artifact recovery is not a trusted financial settlement or durable
        # APPLY/STOP acknowledgement. Keep the original usage reservation
        # untouched until the authoritative M11/M13 reconciliation completes.
        now = utc_now()
        generated_seconds = max(1, int(duration_seconds + 0.999999))

        existing_asset = db.execute(
            select(DBAssetVersion).where(
                DBAssetVersion.project_id == project_id,
                DBAssetVersion.tenant_id == context.tenant_id,
                DBAssetVersion.media_id == orphan_media_id,
            )
        ).scalar_one_or_none()
        if existing_asset is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORPHAN_MEDIA_ALREADY_HAS_ASSET_VERSION",
                    "message": "该媒资已有资产版本但任务未绑定，需先进行人工对账",
                },
            )

        # G70/G71: legacy project materials must be an array of object rows.
        # Treat an invalid JSON shape as conflicting evidence before *any*
        # quota/asset mutation. Converting a dict or string with list(...) can
        # silently append corrupt rows and publish an unrecoverable media list.
        if project.materials is not None and (
            not isinstance(project.materials, list)
            or any(not isinstance(item, dict) for item in project.materials)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORPHAN_MEDIA_PROJECT_MATERIAL_INVALID",
                    "message": "项目现存素材列表格式异常，保持 HELD，需人工核对",
                },
            )

        # G70/G71 FAKE-only: a legacy project can contain two material rows
        # for the same orphan media ID. A single matching first row cannot
        # certify that the second row is safe: downstream consumers may pick
        # a different row and the recovery may double-account storage.
        # Preserve HELD and require manual reconciliation of ambiguous history.
        matching_materials = [
            item for item in (project.materials or [])
            if item.get("id") == orphan_media_id
        ]
        if len(matching_materials) > 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORPHAN_MEDIA_PROJECT_MATERIAL_CONFLICT",
                    "message": "项目存在重复原始媒资记录，保持 HELD，需人工对账",
                },
            )

        # A single same-ID legacy material may already have debited tenant
        # storage. ID/MIME/size equality is not a trusted prior-debit receipt.
        # Never unconditionally reserve a second storage charge; keep HELD
        # and let Owner/M11 reconcile the original accounting evidence.
        if len(matching_materials) == 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ORPHAN_MEDIA_PROJECT_MATERIAL_CONFLICT",
                    "message": "原始媒资已存在但历史存储占用未核实，保持 HELD，需人工对账",
                },
            )

        storage_reservation = db.execute(
            update(DBTenant)
            .where(
                DBTenant.id == context.tenant_id,
                DBTenant.used_storage_bytes + actual_size <= DBTenant.storage_quota_bytes,
            )
            .values(
                used_storage_bytes=DBTenant.used_storage_bytes + actual_size,
                updated_at=utc_now(),
            )
        )
        if storage_reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "STORAGE_QUOTA_EXCEEDED",
                    "message": "恢复原始媒资将超过团队存储额度，任务继续保持 HELD",
                },
            )

        materials = list(project.materials or [])
        material = {
            "id": orphan_media_id,
            "name": f"generation-{task.id}.mp4",
            "url": f"/api/video-use/media/{project_id}/{orphan_media_id}",
            "type": "video",
            "contentType": "video/mp4",
            "duration": {
                "value": max(1, round(duration_seconds * 24_000)),
                "timescale": 24_000,
            },
            "sizeBytes": actual_size,
        }
        project.materials = [*materials, material]
        project.revision += 1
        project.updated_at = utc_now()

        next_version = (
            db.execute(
                select(func.max(DBAssetVersion.version_no)).where(
                    DBAssetVersion.project_id == project_id,
                    DBAssetVersion.media_id == orphan_media_id,
                )
            ).scalar_one_or_none()
            or 0
        ) + 1
        asset_probe = {
            **probe_metadata,
            "provenance": {
                "provider": task.provider,
                "providerArtifactId": task.provider_artifact_id,
                "generationTaskId": task.id,
                "attempt": task.attempts,
                "capabilitySnapshotHash": task.capability_snapshot_hash,
            },
        }
        asset = DBAssetVersion(
            id=str(uuid.uuid4()),
            tenant_id=task.tenant_id,
            project_id=project_id,
            media_id=orphan_media_id,
            version_no=next_version,
            sha256=actual_sha256,
            media_type="video",
            content_type="video/mp4",
            size_bytes=actual_size,
            probe_json=asset_probe,
            created_by=task.requested_by,
            created_at=now,
        )
        db.add(asset)

        previous_status = task.status
        task.status = "RIGHTS_BLOCKED"
        task.progress = 100
        task.message = "原始生成产物已恢复入库；用量预留保持HELD，等待M11/M13可信对账及权利审核"
        task.asset_version_id = asset.id
        task.media_id = orphan_media_id
        task.error_code = None
        task.error_message = None
        task.completed_at = now
        task.updated_at = now
        task.lease_owner = None
        task.lease_expires_at = None
        attempt.status = "RIGHTS_BLOCKED"
        attempt.reconciliation_state = "ARTIFACT_INGESTED"
        attempt.error_code = None
        attempt.error_message = None
        attempt.completed_at = now

        add_generation_event(
            db,
            task,
            attempt=attempt,
            event_type="ARTIFACT_RECOVERY_COMPLETED",
            actor_type="OWNER",
            actor_id=context.user_id,
            from_status=previous_status,
            to_status="RIGHTS_BLOCKED",
            metadata={
                "reason": req.reason,
                "source": "VERIFIED_EXISTING_SIDECAR_OBJECT",
                "upstreamJobId": task.upstream_job_id,
                "providerArtifactId": task.provider_artifact_id,
                "mediaId": orphan_media_id,
                "sha256": actual_sha256,
                "sizeBytes": actual_size,
                "assetVersionId": asset.id,
                "generatedSecondsCandidate": generated_seconds,
                "usageReservationState": "RESERVED_HELD",
                "m11M13Reconciliation": "PENDING",
            },
            now=now,
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
        # G73: keyset-page beyond the first 100 queued rows.  A fixed
        # top-100 scan can starve an eligible task indefinitely when older
        # rows have a different provider mode or revoked rights.
        # Keep the existing FIFO order, lease checks and SKIP LOCKED semantics.
        def iter_claim_candidates() -> Generator[DBGenerationTask, None, None]:
            cursor_created: datetime.datetime | None = None
            cursor_id: str | None = None
            while True:
                claim_query = (
                    select(DBGenerationTask)
                    .where(
                        DBGenerationTask.status.in_(ACTIVE_GENERATION_STATES),
                        or_(
                            DBGenerationTask.lease_expires_at.is_(None),
                            DBGenerationTask.lease_expires_at < now,
                        ),
                        DBGenerationTask.cancel_requested_at.is_(None),
                    )
                    .order_by(DBGenerationTask.created_at.asc(), DBGenerationTask.id.asc())
                    .limit(100)
                )
                if cursor_created is not None and cursor_id is not None:
                    claim_query = claim_query.where(
                        or_(
                            DBGenerationTask.created_at > cursor_created,
                            (
                                (DBGenerationTask.created_at == cursor_created)
                                & (DBGenerationTask.id > cursor_id)
                            ),
                        )
                    )
                if app_engine.dialect.name != "sqlite":
                    claim_query = claim_query.with_for_update(skip_locked=True)
                page = list(db.execute(claim_query).scalars())
                if not page:
                    return
                cursor_created, cursor_id = page[-1].created_at, page[-1].id
                yield from page
                if len(page) < 100:
                    return
        task: DBGenerationTask | None = None
        claim_attestation: DBGenerationProviderAttestation | None = None
        for candidate in iter_claim_candidates():
            # G73: legacy/corrupt JSON may still appear in the durable queue.
            # A single malformed row must not crash every Worker claim or starve
            # later healthy tasks. Never claim it or mutate its lease/usage.
            candidate_snapshot = candidate.capability_snapshot_json
            if not isinstance(candidate_snapshot, dict):
                continue
            # G73: reject corrupt nested policy before leasing any mode. A
            # malformed legacy policy cannot be safely consumed by a Worker.
            # Missing policy retains the existing historical compatibility path.
            candidate_policy = candidate_snapshot.get("providerPolicy")
            if candidate_policy is not None and not isinstance(candidate_policy, dict):
                continue
            # G73: a persisted snapshot may have an invalid quota object even
            # when its outer JSON is valid. Never lease such an artifact:
            # final intake must not interpret malformed limits after Sidecar
            # persistence. This validates shape only; no quota is altered.
            candidate_quota = candidate_snapshot.get("quota")
            if not isinstance(candidate_quota, dict):
                continue
            generated_seconds_limit = candidate_quota.get("monthlyGeneratedSecondsLimit")
            if type(generated_seconds_limit) is not int or generated_seconds_limit <= 0:
                continue
            # G73: malformed persisted failure-window/threshold/cooldown data
            # cannot get a lease and later crash failure recording for all tasks.
            if not generation_failure_policy_valid(candidate_snapshot):
                continue
            # G73: any provider mode can inherit legacy/corrupt JSON.  Do
            # not lease a task whose request cannot be consumed by a Worker.
            candidate_request = candidate.request_json
            if not isinstance(candidate_request, dict):
                continue
            # G73: a legacy task can reference an attempt number with no
            # persisted attempt record. Never lease that task: downstream
            # current_generation_attempt() would raise and starve the queue.
            # Read-only validation; do not repair protected usage/attempt data.
            candidate_attempt = db.execute(
                select(DBGenerationAttempt).where(
                    DBGenerationAttempt.generation_task_id == candidate.id,
                    DBGenerationAttempt.attempt_no == candidate.attempts,
                )
            ).scalar_one_or_none()
            if candidate_attempt is None:
                continue
            # G73: a legacy/corrupt QUEUED task whose current attempt is
            # already terminal must not be claimed and silently re-submitted.
            # Preserve the disputed attempt/usage records for reconciliation;
            # do not invent a refund or transition policy.
            # An active task may reference a terminal/ambiguous attempt after
            # crash, legacy repair or out-of-order persistence.  Never re-lease
            # such a row, including SUBMITTING/RUNNING/INGESTING tasks; leave the
            # disputed attempt and reservations untouched for reconciliation.
            if candidate_attempt.status in TERMINAL_GENERATION_STATES:
                continue
            # G73: task and current-attempt states are committed together.
            # A nonterminal disagreement (including an unknown legacy value)
            # is not authority to recover or submit upstream work again.
            # Quarantine read-only; do not change leases, receipts or quota.
            if candidate_attempt.status != candidate.status:
                continue
            # G70/G71 + G73: the current QUEUED attempt alone is not proof
            # that a prior paid submission was never accepted. A migrated or
            # legacy retry can clear task-level upstream IDs while an older
            # attempt retains HELD/started/accepted evidence. The Owner
            # cancel/retry paths already fence this, but the Worker claim
            # must independently enforce the same invariant before leasing.
            # Fail closed on gaps, zero/renumbered attempts and future rows;
            # never repair usage or call Provider while reconciling history.
            if not generation_prior_history_safe(db, candidate):
                continue
            # G73: QUEUED is safe to submit only for an attempt that has
            # *never* entered upstream submission.  A historical/crashed
            # QUEUED row can retain durable submission evidence even when
            # the task and attempt statuses both say QUEUED.  Re-leasing it
            # would incorrectly return submissionConsumed=False and risk
            # double submission. Preserve all such rows for reconciliation.
            if candidate.status == "QUEUED" and (
                candidate_attempt.submission_started_at is not None
                or candidate_attempt.reconciliation_state != "NOT_SUBMITTED"
                or candidate_attempt.upstream_job_id is not None
                or candidate.upstream_job_id is not None
            ):
                continue
            # G73: an active legacy task without a persisted submit-start marker
            # or usable task-level upstream ID cannot safely be re-leased.
            # It would return submissionConsumed=False and could be resubmitted.
            # Also reject conflicting attempt-level upstream identity. Keep all
            # historical attempt, lease, usage and audit records unchanged.
            if candidate.status != "QUEUED" and (
                (
                    not candidate.upstream_job_id
                    and candidate_attempt.submission_started_at is None
                )
                or (
                    candidate_attempt.upstream_job_id
                    and candidate_attempt.upstream_job_id != candidate.upstream_job_id
                )
            ):
                continue
            candidate_mode = candidate_snapshot.get("mode")
            if candidate_mode != created_app.state.generation_provider_mode:
                continue
            # G72/G73: the normal Provider path must not launder a persisted
            # FAKE talking-head request around its source/REMIX rights fence.
            # Legitimate nonproduction moneyprinter tasks retain FAKE mode;
            # only unknown providers or the fake-only marker on moneyprinter
            # are rejected. Never advance a lease or call an external Provider.
            if candidate.provider not in {"moneyprinter", "deterministic-fake-talking-head"}:
                continue
            if candidate.provider == "moneyprinter" and candidate_request.get("fakeTaskType") is not None:
                continue
            if candidate.provider == "deterministic-fake-talking-head":
                # G72: a stale/corrupt historical usage ledger is not proof of
                # an active reservation. Quarantine before consuming a Worker
                # lease or submitting to the FAKE-only Sidecar. In particular,
                # never reclaim a released/settled reservation, a different
                # tenant/project/attempt, or a missing/changed reservation key.
                # This is only a read-only claim fence, not an M11 refund,
                # Owner disposition policy or protected-history repair.
                usage_rows = list(db.execute(
                    select(DBGenerationUsageEntry).where(
                        DBGenerationUsageEntry.task_id == candidate.id,
                    )
                ).scalars())
                if (
                    len(usage_rows) != 1
                    or any(
                        usage.kind != "RESERVED"
                        or usage.tenant_id != candidate.tenant_id
                        or usage.project_id != candidate.project_id
                        or usage.attempt_id != candidate_attempt.id
                        or usage.reservation_key != f"generation:{candidate.id}"
                        or usage.request_units != 1
                        or usage.generated_seconds != 0
                        for usage in usage_rows
                    )
                ):
                    continue
                # G72: a queued FAKE task is not a standing authority to act
                # after the originating Owner is deactivated/downgraded or the
                # project no longer belongs to that Owner. This is a read-only
                # revocation fence; no grant/delegation policy is selected.
                active_originator = db.execute(
                    select(DBUser.id).where(
                        DBUser.id == candidate.requested_by,
                        DBUser.tenant_id == candidate.tenant_id,
                        DBUser.role == "owner",
                        DBUser.is_active.is_(True),
                    )
                ).scalar_one_or_none()
                original_project_owner = db.execute(
                    select(DBProject.id).where(
                        DBProject.id == candidate.project_id,
                        DBProject.tenant_id == candidate.tenant_id,
                        DBProject.owner_id == candidate.requested_by,
                    )
                ).scalar_one_or_none()
                if active_originator is None or original_project_owner is None:
                    continue
                # Recheck the immutable image version and latest REMIX rights
                # when a Worker claims a FAKE-only task; NEVER allow a real mode.
                stored = candidate_request
                if candidate_mode != "deterministic-fake" or stored.get("fakeTaskType") != "talking-head":
                    continue
                # G72 FAKE-only promises are persisted by the typed Owner
                # creation route. Refuse poisoned/legacy rows that attempt to
                # relabel simulated work as publishable, omit explicit consent,
                # or mutate the single-output contract before any Worker lease.
                # This is a nonproduction safety fence, not an Owner policy change.
                if (
                    stored.get("simulatedOutput") is not True
                    or stored.get("publishable") is not False
                    or stored.get("confirmSyntheticTestOnly") is not True
                    or type(stored.get("outputCount")) is not int
                    or stored["outputCount"] != 1
                ):
                    continue
                # Corrupt nested JSON may be a list/dict even when the outer
                # request is a dict. Never bind it as an asset identifier: a
                # DBAPI type error would starve later healthy Worker claims.
                image_asset_id = stored.get("imageAssetId")
                source_sha256 = stored.get("sourceAssetSha256")
                if (
                    not isinstance(image_asset_id, str) or not image_asset_id
                    or not isinstance(source_sha256, str) or not source_sha256
                ):
                    continue
                # Preserve the original single immutable image version in the
                # typed FAKE-only request. A forged list must not reach the
                # Worker or its eventual artifact evidence.
                if stored.get("inputAssetVersionIds") != [image_asset_id]:
                    continue
                if not fake_talking_head_creation_evidence_matches(db, candidate, stored):
                    continue
                source = db.execute(select(DBAssetVersion).where(
                    DBAssetVersion.id == image_asset_id,
                    DBAssetVersion.project_id == candidate.project_id,
                    DBAssetVersion.tenant_id == candidate.tenant_id,
                )).scalar_one_or_none()
                if (
                    source is None or source.media_type != "image"
                    or source.sha256 != source_sha256
                    or not rights_decision(latest_rights_snapshot(db, source.id, "REMIX"), now)["allowed"]
                ):
                    continue
            if candidate_mode == "moneyprinter":
                config = published_generation_config(db, candidate.tenant_id)
                if (
                    config is None
                    or config.id != candidate.capability_snapshot_json.get("configVersionId")
                    or config.policy_hash != candidate.capability_snapshot_json.get("policyHash")
                    or not isinstance(config.policy_json, dict)
                    or not config.policy_json.get("enabledIntent")
                ):
                    continue
                claim_attestation = db.execute(
                    select(DBGenerationProviderAttestation)
                    .where(
                        DBGenerationProviderAttestation.tenant_id == candidate.tenant_id,
                        DBGenerationProviderAttestation.provider == "moneyprinter",
                        DBGenerationProviderAttestation.worker_id == x_worker_id,
                        DBGenerationProviderAttestation.operator_mode == "moneyprinter",
                        DBGenerationProviderAttestation.config_version_id == config.id,
                        DBGenerationProviderAttestation.policy_hash == config.policy_hash,
                        DBGenerationProviderAttestation.upstream_pin == MONEYPRINTER_UPSTREAM_PIN,
                        DBGenerationProviderAttestation.healthy.is_(True),
                        DBGenerationProviderAttestation.expires_at > now,
                    )
                    .order_by(DBGenerationProviderAttestation.checked_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if claim_attestation is None:
                    continue
                credential_state, network_isolation, canary_profile = (
                    attestation_canary_proof(claim_attestation)
                )
                if (
                    credential_state != "PRESENT"
                    or network_isolation != "ENFORCED"
                    or canary_profile != PRIVATE_CANARY_PROFILE
                    or not private_canary_policy_valid(config.policy_json)
                ):
                    claim_attestation = None
                    continue
            elif candidate_mode != "deterministic-fake":
                continue
            # G75: only an eligible, attested candidate may advance a
            # tenant's OPEN circuit to HALF_OPEN. An ineligible foreign
            # tenant candidate must never change circuit state merely because
            # another tenant's Worker claim commits this transaction.
            candidate_circuit = generation_circuit(db, candidate.tenant_id, create=True, for_update=True)
            if candidate_circuit is not None:
                # Never hand a Provider lease to a tenant whose stored
                # failure evidence is malformed. This check is read-only.
                if not generation_failure_history_valid(candidate_circuit.failure_timestamps_json):
                    continue
                if candidate_circuit.state == "DISABLED":
                    continue
                if candidate_circuit.state == "OPEN":
                    cooldown_until = _as_utc(candidate_circuit.cooldown_until)
                    if cooldown_until is None or now < cooldown_until:
                        continue
                    candidate_circuit.state = "HALF_OPEN"
                    candidate_circuit.half_open_task_id = None
                    candidate_circuit.half_open_lease_epoch = None
                    candidate_circuit.half_open_issuer = None
                    candidate_circuit.updated_at = now
                    add_provider_event(
                        db, tenant_id=candidate.tenant_id,
                        event_type="CIRCUIT_HALF_OPENED", actor_type="SYSTEM",
                        actor_id="api", now=now,
                    )
                if candidate_circuit.state == "HALF_OPEN":
                    if candidate_circuit.half_open_task_id not in {None, candidate.id}:
                        continue
            task = candidate
            break
        if task is None:
            db.rollback()
            response.status_code = 204
            return response

        # Every claim gets a durable, monotonically increasing lease epoch.
        # A recycled worker ID cannot authorize a stale response from an older claim.
        task.lease_epoch = int(task.lease_epoch or 0) + 1
        task.circuit_probe_epoch = None
        task_circuit = generation_circuit(db, task.tenant_id, for_update=True)
        if task_circuit is not None and task_circuit.state == "HALF_OPEN":
            if task_circuit.half_open_task_id not in {None, task.id}:
                db.rollback()
                response.status_code = 204
                return response
            task_circuit.probe_epoch = int(task_circuit.probe_epoch or 0) + 1
            task_circuit.half_open_task_id = task.id
            task_circuit.half_open_lease_epoch = task.lease_epoch
            task_circuit.half_open_issuer = x_worker_id
            task.circuit_probe_epoch = task_circuit.probe_epoch

        previous = task.status
        attempt = current_generation_attempt(db, task)
        submission_consumed = (
            task.status != "QUEUED"
            and not task.upstream_job_id
            and attempt.submission_started_at is not None
        )
        if task.status == "QUEUED" and not task.upstream_job_id:
            task.status = "SUBMITTING"
            task.message = "工作节点正在提交生成任务"
            attempt.status = "SUBMITTING"
            attempt.submission_started_at = attempt.submission_started_at or now
            submission_consumed = False
        elif submission_consumed:
            task.message = "当前尝试的提交权利已被消费，禁止再次提交"
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
            "submissionConsumed": submission_consumed,
            "providerArtifactId": task.provider_artifact_id,
            "providerMode": (
                "talking-head-fake" if task.provider == "deterministic-fake-talking-head"
                else task.capability_snapshot_json.get("mode")
            ),
            "configVersionId": task.capability_snapshot_json.get("configVersionId"),
            "policyHash": task.capability_snapshot_json.get("policyHash"),
            "providerPolicy": task.capability_snapshot_json.get("providerPolicy"),
            "workerProof": {
                "attestationId": claim_attestation.id if claim_attestation else None,
                "expiresAt": iso_utc(claim_attestation.expires_at) if claim_attestation else None,
                "adapterVersion": claim_attestation.adapter_version if claim_attestation else MONEYPRINTER_ADAPTER_VERSION,
                "upstreamPin": claim_attestation.upstream_pin if claim_attestation else MONEYPRINTER_UPSTREAM_PIN,
                "credentialState": attestation_canary_proof(claim_attestation)[0],
                "networkIsolation": attestation_canary_proof(claim_attestation)[1],
                "canaryProfile": attestation_canary_proof(claim_attestation)[2],
            },
            "leaseSeconds": lease_seconds,
            "leaseEpoch": task.lease_epoch,
            "circuitProbeEpoch": task.circuit_probe_epoch,
        }

    @created_app.post("/internal/generation-tasks/{task_id}/heartbeat")
    def heartbeat_generation_task(
        task_id: str,
        _req: GenerationWorkerHeartbeatRequest,
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        x_worker_lease_epoch: int | None = Header(default=None, alias="X-Worker-Lease-Epoch"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        begin_serialized_write(db)
        task = generation_task_by_id(db, task_id, for_update=True)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if x_worker_lease_epoch is None or x_worker_lease_epoch != task.lease_epoch:
            raise HTTPException(status_code=409, detail={"code": "LEASE_EPOCH_MISMATCH", "message": "工作节点租约版本已过期"})
        if task.status not in ACTIVE_GENERATION_STATES or task.cancel_requested_at is not None:
            raise HTTPException(status_code=409, detail={"code": "TASK_NOT_ACTIVE", "message": "任务已不再处于可续租状态"})
        # G70/G71/G73: a lease heartbeat is not a fresh STOP/APPLY ACK.
        # Older attempt receipts may change after claim.  Do not prolong a
        # lease with unresolved upstream evidence: refuse before any mutation,
        # preserving HELD/RESERVED and the prior lease for trusted review.
        if not generation_prior_history_safe(db, task):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_PRIOR_ATTEMPT_RECONCILIATION_REQUIRED",
                    "message": "历史生成尝试证据在领取后异常，拒绝续租并保持账本",
                },
            )
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
        x_worker_lease_epoch: int | None = Header(default=None, alias="X-Worker-Lease-Epoch"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        begin_serialized_write(db)
        task = generation_task_by_id(db, task_id, for_update=True)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if x_worker_lease_epoch is None or x_worker_lease_epoch != task.lease_epoch:
            raise HTTPException(status_code=409, detail={"code": "LEASE_EPOCH_MISMATCH", "message": "工作节点租约版本已过期"})
        if task.cancel_requested_at is not None or task.status == "CANCELED":
            raise HTTPException(status_code=409, detail={"code": "TASK_CANCELED", "message": "任务已取消，迟到响应已隔离"})
        if req.status != task.status and req.status not in WORKER_TRANSITIONS.get(task.status, set()):
            raise HTTPException(status_code=409, detail={"code": "ILLEGAL_GENERATION_TRANSITION", "message": "生成任务状态迁移不合法"})
        if req.upstreamJobId and task.upstream_job_id and req.upstreamJobId != task.upstream_job_id:
            raise HTTPException(status_code=409, detail={"code": "UPSTREAM_ID_IMMUTABLE", "message": "上游任务编号不可替换"})
        if req.providerArtifactId and task.provider_artifact_id and req.providerArtifactId != task.provider_artifact_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "PROVIDER_ARTIFACT_ID_IMMUTABLE", "message": "上游产物编号不可替换"},
            )
        previous = task.status
        attempt = current_generation_attempt(db, task)
        # G73: a Worker transition must not convert a legacy/partially
        # persisted current Attempt out of a terminal or divergent status.
        # Both statuses are written together by the normal transition path;
        # leave conflicting history, usage and lease untouched for review.
        if attempt.status != task.status:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_ATTEMPT_STATUS_CONFLICT",
                    "message": "任务与当前尝试的持久状态不一致，须先人工核验",
                },
            )
        # G70/G71/G73: history may change after the Worker claim was issued.
        # A valid lease is not an independent receipt for an older paid attempt.
        # Recheck under the task-row lock before status/usage/audit mutations.
        if not generation_prior_history_safe(db, task):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_PRIOR_ATTEMPT_RECONCILIATION_REQUIRED",
                    "message": "历史生成尝试证据在领取后异常，停止Worker状态迁移并保持账本",
                },
            )
        # G73: A persisted Attempt-level upstream receipt is independent
        # evidence of an already submitted job. If it diverges from the task
        # receipt (including a missing task-level ID), a leased Worker must
        # not transition, replace the job identity, or settle/release usage.
        # Preserve both records for explicit reconciliation: FAKE-only,
        # read-only refusal before status, quota or event mutation.
        if (
            attempt.upstream_job_id is not None
            and attempt.upstream_job_id != task.upstream_job_id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "UPSTREAM_ATTEMPT_IDENTITY_CONFLICT",
                    "message": "任务与尝试的持久上游回执不一致，须先人工核验",
                },
            )
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
        # G70/G71: once the original upstream job is known and an artifact
        # transfer fails, the provider may already have charged for the output.
        # Keep the existing generation reservation HELD; never record RELEASED
        # or allow the generic retry path to create a second paid attempt.
        artifact_recovery_hold = bool(
            req.status == "FAILED"
            and task.upstream_job_id
            and req.errorCode
            and str(req.errorCode).startswith("ARTIFACT_")
        )
        if artifact_recovery_hold:
            task.error_code = "ARTIFACT_INGEST_FAILED"
            attempt.error_code = task.error_code
            attempt.reconciliation_state = "HELD"
        if req.status == "UNKNOWN":
            attempt.reconciliation_state = "UNKNOWN"
        if req.status in {"FAILED", "CANCELED", "UNKNOWN", "PARTIAL"}:
            if (
                req.status == "FAILED"
                and not req.retryable
                and task.error_code
                and not artifact_recovery_hold
            ):
                task.error_code = f"NON_RETRYABLE_{task.error_code}"
                attempt.error_code = task.error_code
            task.completed_at = now
            attempt.completed_at = now
            task.lease_owner = None
            task.lease_expires_at = None
            provider_failure = bool(
                req.errorCode
                and (
                    req.errorCode.startswith("PROVIDER_")
                    or req.errorCode in {"AMBIGUOUS_SUBMISSION", "STATUS_TIMEOUT"}
                )
            )
            if provider_failure:
                record_generation_failure(db, task, now=now)
            if req.status == "CANCELED" or (
                req.status == "FAILED" and (not req.retryable or task.attempts >= task.max_attempts)
            ):
                if not artifact_recovery_hold:
                    release_generation_reservation(
                        db, task, attempt,
                        reason_code=req.errorCode or req.status, now=now,
                    )
        else:
            task.lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        transition_metadata = {"progress": req.progress}
        if artifact_recovery_hold:
            transition_metadata.update({
                "originalErrorCode": req.errorCode,
                "reconciliationState": "HELD",
                "usageReservation": "HELD",
                "upstreamJobId": task.upstream_job_id,
                "providerArtifactId": task.provider_artifact_id,
            })
        add_generation_event(
            db, task, attempt=attempt, event_type="WORKER_TRANSITION",
            actor_type="WORKER", actor_id=x_worker_id,
            from_status=previous, to_status=req.status,
            metadata=transition_metadata, now=now,
        )
        db.commit()
        return {"taskId": task.id, "status": task.status, "progress": task.progress}

    @contextmanager
    def postgres_storage_hold_liveness(lock_key: int):
        """Hold a dedicated PostgreSQL advisory lock throughout the intake.

        The quota marker commits before Sidecar I/O; this session-level lock
        survives that commit but disappears on an actual process SIGKILL.
        """
        if app_engine.dialect.name != "postgresql":
            yield
            return
        # The request DB session already holds the task row lock. A second
        # checkout may exhaust a bounded PostgreSQL pool. Return a retryable,
        # fail-closed response rather than an unhandled 500 or quota mutation.
        try:
            liveness_conn = app_engine.connect()
        except SQLAlchemyPoolTimeout as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_HOLD_DB_CAPACITY_UNAVAILABLE",
                        "message": "存储预留保护连接暂不可用，请稍后重试"},
            ) from exc
        with liveness_conn as conn:
            acquired = False
            try:
                # Never block a worker indefinitely on a session-level lock.
                # A colliding or externally held key is a retryable, non-mutating
                # capacity condition; the quota hold is not opened yet.
                obtained = conn.execute(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": lock_key},
                ).scalar_one()
                if obtained is not True:
                    conn.rollback()
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "STORAGE_HOLD_LOCK_CONTENDED",
                                "message": "存储预留保护锁暂被占用，请稍后重试"},
                    )
                # Mark acquired before COMMIT: even a failed commit must try
                # to unlock or invalidate the session before pool reuse.
                acquired = True
                conn.commit()
                yield
            finally:
                if acquired:
                    try:
                        unlocked = conn.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
                        ).scalar_one()
                        conn.commit()
                        if unlocked is not True:
                            raise RuntimeError("storage hold liveness lock could not be released")
                    except BaseException:
                        conn.invalidate()
                        raise

    def postgres_storage_hold_is_live(lock_key: int) -> bool:
        """A freed advisory lock proves no active holder; not permission to refund."""
        if type(lock_key) is not int or lock_key < 0 or lock_key > 0x7FFFFFFFFFFFFFFF:
            return False
        try:
            probe_conn = app_engine.connect()
        except SQLAlchemyPoolTimeout as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "STORAGE_HOLD_DB_CAPACITY_UNAVAILABLE",
                        "message": "无法确认历史预留的存活状态，请稍后重试"},
            ) from exc
        with probe_conn as probe:
            obtained = probe.execute(
                text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key}
            ).scalar_one()
            if obtained is True:
                try:
                    released = probe.execute(
                        text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
                    ).scalar_one()
                    probe.commit()
                    if released is not True:
                        raise RuntimeError("orphan probe liveness lock could not be released")
                except BaseException:
                    probe.invalidate()
                    raise
                return False
            probe.rollback()
            return True

    @created_app.post("/internal/generation-tasks/{task_id}/artifact-intake", status_code=201)
    def intake_generation_artifact(
        task_id: str,
        providerArtifactId: str = Form(..., min_length=1, max_length=256),
        file: UploadFile = File(...),
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        x_worker_lease_epoch: int | None = Header(default=None, alias="X-Worker-Lease-Epoch"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        if any(marker in providerArtifactId for marker in ("://", "..", "\\", "/")):
            raise HTTPException(status_code=422, detail={"code": "ARTIFACT_ID_INVALID", "message": "产物编号格式不安全"})
        task = generation_task_by_id(db, task_id, for_update=True)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
        if task.provider == "deterministic-fake-talking-head":
            # G72: revoking the original Owner or changing project ownership
            # after Worker claim must fence artifact intake before quota, Sidecar
            # upload or audit mutation. An internal Worker token does not grant
            # authority to impersonate a deactivated task initiator.
            active_originator = db.execute(
                select(DBUser.id).where(
                    DBUser.id == task.requested_by,
                    DBUser.tenant_id == task.tenant_id,
                    DBUser.role == "owner",
                    DBUser.is_active.is_(True),
                )
            ).scalar_one_or_none()
            original_project_owner = db.execute(
                select(DBProject.id).where(
                    DBProject.id == task.project_id,
                    DBProject.tenant_id == task.tenant_id,
                    DBProject.owner_id == task.requested_by,
                )
            ).scalar_one_or_none()
            if active_originator is None or original_project_owner is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED",
                        "message": "原口播任务发起人或项目归属已失效，模拟产物隔离",
                    },
                )
            # A task leased on a previous build can contain malformed durable
            # request JSON. Refuse intake before querying with a non-scalar
            # source ID; do not mutate the lease, storage, or usage ledger.
            stored_request = task.request_json
            if not isinstance(stored_request, dict):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FAKE_TALKING_HEAD_REQUEST_INVALID",
                            "message": "口播任务请求结构异常，模拟产物已隔离"},
                )
            source_id = stored_request.get("imageAssetId")
            source_sha256 = stored_request.get("sourceAssetSha256")
            if (
                not isinstance(source_id, str) or not source_id
                or not isinstance(source_sha256, str) or not source_sha256
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FAKE_TALKING_HEAD_REQUEST_INVALID",
                            "message": "口播素材字段异常，模拟产物已隔离"},
                )
            # G72: claim-time FAKE-only consent/nonpublication/input binding
            # must remain true at intake, even for a row changed after lease.
            # Reject before any storage reservation or Sidecar upload.
            if (
                stored_request.get("fakeTaskType") != "talking-head"
                or stored_request.get("simulatedOutput") is not True
                or stored_request.get("publishable") is not False
                or stored_request.get("confirmSyntheticTestOnly") is not True
                or type(stored_request.get("outputCount")) is not int
                or stored_request["outputCount"] != 1
                or stored_request.get("inputAssetVersionIds") != [source_id]
                or not fake_talking_head_creation_evidence_matches(db, task, stored_request)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FAKE_TALKING_HEAD_REQUEST_INVALID",
                            "message": "口播FAKE任务持久合约已变化，模拟产物已隔离"},
                )
            source = db.execute(select(DBAssetVersion).where(
                DBAssetVersion.id == source_id,
                DBAssetVersion.tenant_id == task.tenant_id,
                DBAssetVersion.project_id == task.project_id,
            )).scalar_one_or_none()
            if (
                source is None or source.media_type != "image"
                or source.sha256 != source_sha256
                or not rights_decision(latest_rights_snapshot(db, source.id, "REMIX"), utc_now())["allowed"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FAKE_TALKING_HEAD_SOURCE_REVOKED", "message": "输入图片授权或版本已失效，模拟产物隔离"},
                )
            # A previously revoked upload may have returned its storage debit
            # with a valid receipt, but Owner reactivation is NOT a recovery ACK.
            # Fail closed on the durable orphan audit; a Worker must never
            # silently re-upload or settle the old FAKE artifact after restoration.
            previous_revocation_failures = db.execute(
                select(DBGenerationEvent.metadata_json).where(
                    DBGenerationEvent.generation_task_id == task.id,
                    DBGenerationEvent.event_type == "ARTIFACT_INGEST_FAILED",
                )
            ).scalars().all()
            if any(
                isinstance(evidence, dict)
                and evidence.get("reasonCode") == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
                for evidence in previous_revocation_failures
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_RECOVERY_REQUIRED",
                        "message": "此前授权撤销已形成持久孤儿产物，须先人工核验原始产物",
                    },
                )
        if task.asset_version_id:
            # G70/G71: a terminal idempotent ACK must bind the replay's bytes,
            # MIME and scope to the already committed asset. Previously a
            # different payload with the same artifact ID/worker got HTTP 201
            # without its body being checked. This path is read-only.
            completion = db.execute(
                select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == task.id,
                    DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
                    DBGenerationEvent.actor_id == x_worker_id,
                )
            ).scalar_one_or_none()
            existing_asset = db.get(DBAssetVersion, task.asset_version_id)
            if (
                task.provider_artifact_id != providerArtifactId
                or completion is None
                or existing_asset is None
                or existing_asset.tenant_id != task.tenant_id
                or existing_asset.project_id != task.project_id
                or existing_asset.media_id != task.media_id
                or file.content_type != "video/mp4"
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "ARTIFACT_REPLAY_MISMATCH",
                            "message": "已入库产物的重试身份或类型不匹配"},
                )
            file.file.seek(0, os.SEEK_END)
            replay_size = file.file.tell()
            file.file.seek(0)
            if replay_size <= 0 or replay_size > max_upload_bytes or replay_size != existing_asset.size_bytes:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "ARTIFACT_REPLAY_MISMATCH",
                            "message": "已入库产物的重试字节数不匹配"},
                )
            replay_digest = hashlib.sha256()
            while chunk := file.file.read(1024 * 1024):
                replay_digest.update(chunk)
            file.file.seek(0)
            if not hmac.compare_digest(replay_digest.hexdigest(), existing_asset.sha256):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "ARTIFACT_REPLAY_MISMATCH",
                            "message": "已入库产物的重试摘要不匹配"},
                )
            return generation_task_response(
                db, task, rights=generation_rights(db, task), include_history=True
            )
        task_circuit = generation_circuit(db, task.tenant_id)
        if task_circuit is not None and task_circuit.state == "DISABLED":
            raise HTTPException(
                status_code=409,
                detail={"code": "PROVIDER_EMERGENCY_STOPPED", "message": "紧急停机期间迟到产物已隔离"},
            )
        now = utc_now()
        if task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去任务租约"})
        if x_worker_lease_epoch is None or x_worker_lease_epoch != task.lease_epoch:
            raise HTTPException(status_code=409, detail={"code": "LEASE_EPOCH_MISMATCH", "message": "工作节点租约版本已过期"})
        if task.cancel_requested_at is not None or task.status == "CANCELED":
            raise HTTPException(status_code=409, detail={"code": "TASK_CANCELED", "message": "任务已取消，迟到产物已隔离"})
        if task.status not in {"RUNNING", "INGESTING"}:
            raise HTTPException(status_code=409, detail={"code": "ARTIFACT_INTAKE_NOT_ALLOWED", "message": "当前状态不允许接收产物"})
        # G73 FAKE-only: a still-valid Worker lease is not an ACK for a
        # damaged CURRENT attempt.  Status divergence, a HELD/UNKNOWN
        # reconciliation or a contradictory upstream identity can appear after
        # transition. Refuse before storage reservation, Sidecar I/O or audit
        # mutation; do not infer an APPLY/STOP ACK from the task status.
        current_attempt = current_generation_attempt(db, task)
        if (
            current_attempt.status != task.status
            or current_attempt.reconciliation_state in {"HELD", "UNKNOWN"}
            or (
                current_attempt.upstream_job_id is not None
                and current_attempt.upstream_job_id != task.upstream_job_id
            )
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_CURRENT_ATTEMPT_RECONCILIATION_REQUIRED",
                    "message": "当前生成尝试的持久状态或上游回执异常，禁止产物入库并保持账本",
                },
            )
        # G70/G71/G73 FAKE-only: history can become HELD or acquire durable
        # upstream submission evidence after Worker claim and transition.
        # Neither a live lease nor an artifact ID authorizes a new storage
        # debit or Sidecar upload while an earlier attempt is disputed.
        # Fail before byte processing, reservation, upload or audit mutation.
        if not generation_prior_history_safe(db, task):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GENERATION_PRIOR_ATTEMPT_RECONCILIATION_REQUIRED",
                    "message": "历史生成尝试证据在领取后异常，拒绝产物入库并保持账本",
                },
            )
        if task.provider_artifact_id and task.provider_artifact_id != providerArtifactId:
            raise HTTPException(status_code=409, detail={"code": "ARTIFACT_ID_MISMATCH", "message": "产物编号与当前任务回执不一致"})
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

        task_project_id = task.project_id
        task_tenant_id = task.tenant_id
        artifact_idempotency_key = f"generation:{task_id}"
        expected_media_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"aether-video-use:{task_project_id}:{artifact_idempotency_key}",
        ))
        # PostgreSQL session-level liveness remains bound across quota commit
        # and the FAKE Sidecar call, to distinguish active duplicates from
        # crash-orphaned holds even within the same lease generation.
        storage_reservation_event_id = str(uuid.uuid4())
        storage_liveness_lock_key = int(uuid.UUID(storage_reservation_event_id)) & 0x7FFFFFFFFFFFFFFF

        def compensate_artifact_storage_or_hold(tenant_id: str) -> None:
            # G70/G71: a damaged quota counter is not a valid refund. Never
            # clamp it to zero while recording a successful compensation.
            # Keep the durable OPENED hold for human reconciliation instead.
            released = db.execute(
                update(DBTenant)
                .where(
                    DBTenant.id == tenant_id,
                    DBTenant.used_storage_bytes >= upload_size,
                )
                .values(used_storage_bytes=DBTenant.used_storage_bytes - upload_size)
            )
            if released.rowcount != 1:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "持久预留与租户存储额度不一致，须人工核验，禁止宣称已释放",
                    },
                )

        with postgres_storage_hold_liveness(storage_liveness_lock_key):
            # G70/G71: persist each quota hold with its lease epoch BEFORE Sidecar.
            # When a Worker lease changes after an uncertain prior upload, stop
            # cross-epoch replay without a durable release receipt. Same-epoch
            # concurrent intake keeps its existing duplicate/ACK reconciliation;
            # same-epoch OS SIGKILL still requires a separate durable remedy.
            opened_holds = db.execute(
                select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_OPENED",
                )
            ).scalars().all()
            # G70/G71: a RELEASED receipt without any OPENED reservation is
            # orphaned audit evidence, not proof that a storage debit was
            # returned. Never reserve new quota or call Sidecar on that task.
            # Do not reconstruct, refund or erase historical evidence here.
            releases = db.execute(
                select(DBGenerationEvent).where(
                    DBGenerationEvent.generation_task_id == task_id,
                    DBGenerationEvent.event_type == "STORAGE_RESERVATION_RELEASED",
                )
            ).scalars().all()
            if releases and not opened_holds:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "发现无原始预留的持久释放回执，须人工核验原产物及额度",
                    },
                )
            # G70/G71: an unresolved OPENED hold must originate from the same
            # trusted API issuer as an OPENED hold that was later released.
            # Without this check, a forged/migrated actor with matching bytes
            # and a live advisory lock could authorize another storage debit.
            # Fail closed; do not rewrite historical quota or audit evidence.
            if any(
                hold.actor_type != "SYSTEM" or hold.actor_id != "api"
                for hold in opened_holds
            ):
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "存储预留来源不可信，须人工核验原产物与额度",
                    },
                )
            if opened_holds:
                # A durable reservation must refer to an attempt belonging to
                # THIS task. A foreign-task attempt can satisfy every JSON
                # field while incorrectly clearing a hold or allowing a second
                # storage debit. Historical NULL/NULL pairs remain readable;
                # do not synthesize a reconciliation or refund.
                valid_attempt_ids = set(db.execute(
                    select(DBGenerationAttempt.id).where(
                        DBGenerationAttempt.generation_task_id == task_id
                    )
                ).scalars())
                if any(
                    hold.attempt_id is not None
                    and hold.attempt_id not in valid_attempt_ids
                    for hold in opened_holds
                ):
                    db.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "存储预留归属的任务尝试不可信，须人工核验原产物与额度",
                        },
                    )
                # A release event is evidence only when it binds exactly to
                # the opened reservation. Untrusted/migrated JSON must never
                # turn an outstanding quota hold into a presumed release.
                opened_by_id = {hold.id: hold for hold in opened_holds}
                released_ids = set()
                # A release row without a typed reference to an OPENED hold is
                # ambiguous audit evidence even if a different row is valid.
                # Do not let a valid receipt hide an unrelated or corrupt one.
                unmatched_release_reference = False
                # A second receipt for one hold is ambiguous financial evidence,
                # even when either individual receipt looks valid. Never infer
                # quota reconciliation from duplicate audit rows.
                release_reference_counts = {}
                # A referenced RELEASED row with invalid accounting bindings is
                # not an admissible release, even while the OPENED liveness
                # advisory lock is held by a concurrent API process. Otherwise
                # it could be ignored and a second quota hold opened.
                invalid_release_binding = False
                for receipt in releases:
                    released_metadata = receipt.metadata_json
                    if not isinstance(released_metadata, dict):
                        unmatched_release_reference = True
                        continue
                    # Corrupt JSON containers are unhashable; an audit row must
                    # never turn an orphan safety fence into an HTTP 500.
                    opened_reference = released_metadata.get("storageReservationEventId")
                    if type(opened_reference) is not str:
                        unmatched_release_reference = True
                        continue
                    opened = opened_by_id.get(opened_reference)
                    if opened is None:
                        unmatched_release_reference = True
                        continue
                    release_reference_counts[opened_reference] = (
                        release_reference_counts.get(opened_reference, 0) + 1
                    )
                    if not isinstance(opened.metadata_json, dict):
                        invalid_release_binding = True
                        continue
                    opened_metadata = opened.metadata_json
                    binding_fields = (
                        "workerId", "leaseEpoch", "providerArtifactId",
                        "mediaId", "sha256", "reservedStorageBytes",
                    )
                    # Paired absent/malformed identities previously passed via
                    # None == None. A release is admissible only if its OPENED
                    # event is a typed, system-originated reservation.
                    required_opened_strings = ("workerId", "providerArtifactId", "mediaId", "sha256")
                    opened_sha256 = opened_metadata.get("sha256")
                    # Each OPENED marker's advisory-lock identity is derived
                    # from its immutable event ID at reservation time. A
                    # different but well-typed lock key is NOT proof that this
                    # released hold was ever protected by the original API
                    # connection. Malformed legacy IDs fail closed, not 500.
                    expected_opened_lock_key = None
                    if type(opened.id) is str:
                        try:
                            expected_opened_lock_key = (
                                int(uuid.UUID(opened.id)) & 0x7FFFFFFFFFFFFFFF
                            )
                        except ValueError:
                            pass
                    opened_identity_valid = (
                        opened.actor_type == "SYSTEM"
                        and opened.actor_id == "api"
                        and all(
                            type(opened_metadata.get(field)) is str and bool(opened_metadata[field])
                            for field in required_opened_strings
                        )
                        and type(opened_sha256) is str
                        and len(opened_sha256) == 64
                        and all(c in "0123456789abcdef" for c in opened_sha256)
                    )
                    if (
                        opened_identity_valid
                        and receipt.actor_type == "SYSTEM"
                        and receipt.actor_id == "api"
                        and receipt.attempt_id == opened.attempt_id
                        # A release cannot precede the original reservation.
                        # An inverted durable audit timeline is not refund proof.
                        and receipt.created_at >= opened.created_at
                        and type(released_metadata.get("reasonCode")) is str
                        and released_metadata["reasonCode"] in {
                            "DUPLICATE_ARTIFACT_INTAKE", "UPLOAD_INTERRUPTED",
                            "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
                        }
                        # Legacy/migrated OPENED receipts lacking the typed
                        # PG liveness identity cannot clear a durable hold.
                        # A well-formed RELEASED row alone is not a trustworthy
                        # quota refund or a reason to reserve a second time.
                        and type(opened_metadata.get("livenessLockKey")) is int
                        and opened_metadata["livenessLockKey"] == expected_opened_lock_key
                        and type(opened_metadata.get("leaseEpoch")) is int
                        and type(opened_metadata.get("reservedStorageBytes")) is int
                        and opened_metadata["reservedStorageBytes"] > 0
                        and type(released_metadata.get("leaseEpoch")) is int
                        and type(released_metadata.get("reservedStorageBytes")) is int
                        and all(
                            released_metadata.get(field) == opened_metadata.get(field)
                            for field in binding_fields
                        )
                    ):
                        released_ids.add(opened.id)
                    else:
                        invalid_release_binding = True
                if unmatched_release_reference or invalid_release_binding or any(
                    count > 1 for count in release_reference_counts.values()
                ):
                    db.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "存储预留存在重复或无法匹配的释放回执，请人工核验原产物与额度",
                        },
                    )
                # An unresolved hold in the SAME lease epoch is not proof that
                # another request is still live. It may be a SIGKILL orphan.
                # Permit legacy identical concurrent ACK flow only when every
                # outstanding reservation binds to the exact same bytes, worker,
                # artifact and project media. Reject changed bytes before a
                # second quota increment or any Sidecar write. This does NOT
                # reconcile a same-epoch orphan with identical bytes.
                # G70/G71: a live advisory lock proves only that SOME backend
                # holds that number. For an unresolved OPENED event, bind the
                # lock to the immutable reservation ID before trusting a live
                # connection; otherwise an unrelated live lock can permit a
                # second debit against a corrupted historical hold.
                # No automatic refund or release is authorized here.
                for hold in opened_holds:
                    if hold.id in released_ids:
                        continue
                    try:
                        expected_hold_lock_key = (
                            int(uuid.UUID(hold.id)) & 0x7FFFFFFFFFFFFFFF
                        )
                    except (TypeError, ValueError, AttributeError):
                        expected_hold_lock_key = None
                    hold_metadata = hold.metadata_json
                    if (
                        not isinstance(hold_metadata, dict)
                        or type(hold_metadata.get("livenessLockKey")) is not int
                        or hold_metadata["livenessLockKey"] != expected_hold_lock_key
                    ):
                        db.rollback()
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                                "message": "未释放预留的存活锁与原始事件不匹配，须人工核验额度",
                            },
                        )
                incoming_sha256 = digest.hexdigest()
                if any(
                    hold.id not in released_ids
                    and (
                        not isinstance(hold.metadata_json, dict)
                        or type(hold.metadata_json.get("leaseEpoch")) is not int
                        or hold.metadata_json["leaseEpoch"] != x_worker_lease_epoch
                        or hold.metadata_json.get("workerId") != x_worker_id
                        or hold.metadata_json.get("providerArtifactId") != providerArtifactId
                        or hold.metadata_json.get("mediaId") != expected_media_id
                        or type(hold.metadata_json.get("reservedStorageBytes")) is not int
                        or hold.metadata_json["reservedStorageBytes"] != upload_size
                        or hold.metadata_json.get("sha256") != incoming_sha256
                    )
                    for hold in opened_holds
                ):
                    db.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "已有未对账的持久存储预留，禁止重复计费，请人工核验原产物",
                        },
                    )
            if app_engine.dialect.name == "postgresql" and any(
                hold.id not in released_ids
                and not postgres_storage_hold_is_live(
                    hold.metadata_json.get("livenessLockKey")
                    if isinstance(hold.metadata_json, dict) else None
                )
                for hold in opened_holds
            ):
                # A durable receipt with no live connection is either an orphan
                # or legacy/unverifiable. Never silently reserve or refund again.
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "持久预留缺少在途证明，禁止重复占额，请人工核验原产物",
                    },
                )
            reservation = db.execute(
                update(DBTenant)
                .where(
                    DBTenant.id == task_tenant_id,
                    DBTenant.used_storage_bytes >= 0,
                    DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes,
                )
                .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=now)
            )
            if reservation.rowcount != 1:
                # A negative counter is corrupt evidence, not spare capacity.
                stored_bytes = db.execute(
                    select(DBTenant.used_storage_bytes).where(DBTenant.id == task_tenant_id)
                ).scalar_one_or_none()
                db.rollback()
                if stored_bytes is not None and stored_bytes < 0:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "存储计数器异常，须人工对账后才能重新占额",
                        },
                    )
                raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "团队存储空间已达到配额上限"})
            opened_attempt = current_generation_attempt(db, task)
            db.add(DBGenerationEvent(
                id=storage_reservation_event_id,
                generation_task_id=task_id,
                attempt_id=opened_attempt.id if opened_attempt is not None else None,
                event_type="STORAGE_RESERVATION_OPENED",
                from_status=task.status,
                to_status=task.status,
                actor_type="SYSTEM",
                actor_id="api",
                metadata_json={
                    "workerId": x_worker_id,
                    "leaseEpoch": x_worker_lease_epoch,
                    "livenessLockKey": storage_liveness_lock_key,
                    "providerArtifactId": providerArtifactId,
                    "mediaId": expected_media_id,
                    "sha256": digest.hexdigest(),
                    "reservedStorageBytes": upload_size,
                },
                created_at=now,
            ))
            # Marker and aggregate quota increment are one PostgreSQL transaction.
            # A crash can leave both committed, or neither, never only one.
            db.commit()
    
            duplicate_storage_release_event_id = str(uuid.uuid4())
            duplicate_storage_release_requested = False
            storage_release_reason = "DUPLICATE_ARTIFACT_INTAKE"
    
            def release_duplicate_storage_reservation() -> None:
                nonlocal duplicate_storage_release_requested
                duplicate_storage_release_requested = True
                existing_release = db.get(DBGenerationEvent, duplicate_storage_release_event_id)
                if existing_release is not None:
                    existing_bytes = int(
                        (existing_release.metadata_json or {}).get("reservedStorageBytes") or -1
                    )
                    if (
                        existing_release.generation_task_id != task_id
                        or existing_release.event_type != "STORAGE_RESERVATION_RELEASED"
                        or existing_bytes != upload_size
                    ):
                        raise RuntimeError("duplicate storage release receipt does not match this request")
                    return
    
                attempt = current_generation_attempt(db, task)
                db.add(DBGenerationEvent(
                    id=duplicate_storage_release_event_id,
                    generation_task_id=task_id,
                    attempt_id=attempt.id if attempt is not None else None,
                    event_type="STORAGE_RESERVATION_RELEASED",
                    from_status=task.status,
                    to_status=task.status,
                    actor_type="SYSTEM",
                    actor_id="api",
                    metadata_json={
                        "reasonCode": storage_release_reason,
                        "storageReservationEventId": storage_reservation_event_id,
                        "workerId": x_worker_id,
                        "leaseEpoch": x_worker_lease_epoch,
                        "providerArtifactId": providerArtifactId,
                        "mediaId": expected_media_id,
                        "sha256": digest.hexdigest(),
                        "reservedStorageBytes": upload_size,
                    },
                    created_at=utc_now(),
                ))
                released = db.execute(
                    update(DBTenant)
                    .where(
                        DBTenant.id == task_tenant_id,
                        DBTenant.used_storage_bytes >= upload_size,
                    )
                    .values(
                        used_storage_bytes=DBTenant.used_storage_bytes - upload_size,
                        updated_at=utc_now(),
                    )
                )
                if released.rowcount != 1:
                    raise RuntimeError("duplicate storage reservation cannot be reconciled safely")
    
            def persist_duplicate_storage_reservation_release() -> None:
                # The release receipt and aggregate decrement share one transaction.
                # After an ambiguous commit result, read the receipt before retrying.
                for retry_index in range(2):
                    try:
                        release_duplicate_storage_reservation()
                        db.commit()
                        return
                    except Exception:
                        db.rollback()
                        existing_release = db.get(DBGenerationEvent, duplicate_storage_release_event_id)
                        if existing_release is not None:
                            existing_bytes = int(
                                (existing_release.metadata_json or {}).get("reservedStorageBytes") or -1
                            )
                            if (
                                existing_release.generation_task_id != task_id
                                or existing_release.event_type != "STORAGE_RESERVATION_RELEASED"
                                or existing_bytes != upload_size
                            ):
                                raise RuntimeError("duplicate storage release receipt does not match this request")
                            db.rollback()
                            return
                        db.rollback()
                        if retry_index == 1:
                            raise
                        begin_serialized_write(db)
    
            uploaded = None
            media_id = None
            metadata = {}
            try:
                try:
                    uploaded = created_app.state.video_use.upload_media(
                        task_project_id,
                        f"generation-{task_id}.mp4",
                        "video/mp4",
                        file.file,
                        idempotency_key=artifact_idempotency_key,
                        content_sha256=digest.hexdigest(),
                    )
                except BaseException as abrupt_exit:
                    if isinstance(abrupt_exit, Exception):
                        # Ordinary transport errors use the established FAILED/HELD
                        # recovery path; do not change paid-usage semantics.
                        raise
                    # G70/G71: the reservation was committed before the sidecar
                    # call. An in-process abrupt request exit bypasses the normal
                    # Exception compensation path, so release this request's hold
                    # with an atomic DB receipt before propagating the interruption.
                    # This cannot protect against process kill or host crash.
                    db.rollback()
                    begin_serialized_write(db)
                    storage_release_reason = "UPLOAD_INTERRUPTED"
                    persist_duplicate_storage_reservation_release()
                    raise
                # G70/G71/G72: a Sidecar ACK is untrusted media evidence, not
                # permission to coerce its values into an asset/usage ledger.
                # Validate before the final DB transaction; existing failure/HELD
                # compensation handles rejected data without publishing an asset.
                if not isinstance(uploaded, dict):
                    uploaded = None
                    raise ValueError("generated artifact Sidecar response must be an object")
                if (
                    uploaded.get("projectId") != task_project_id
                    or not isinstance(uploaded.get("mediaId"), str)
                    or uploaded["mediaId"] != expected_media_id
                    or (uploaded.get("contentType") is not None
                        and uploaded["contentType"] != "video/mp4")
                    or (uploaded.get("sha256") is not None
                        and uploaded["sha256"] != digest.hexdigest())
                    or (uploaded.get("sizeBytes") is not None
                        and (type(uploaded["sizeBytes"]) is not int
                             or uploaded["sizeBytes"] != upload_size))
                ):
                    raise ValueError("generated artifact Sidecar identity, hash or content type mismatched")
                metadata = uploaded.get("metadata")
                if not isinstance(metadata, dict):
                    raise ValueError("generated artifact Sidecar probe must be an object")
                raw_size = metadata.get("sizeBytes")
                duration = metadata.get("durationSeconds")
                video = metadata.get("video")
                if (
                    type(raw_size) is not int or raw_size != upload_size
                    or type(duration) not in (int, float)
                    or not math.isfinite(duration) or duration <= 0
                    or not isinstance(video, dict)
                    or not isinstance(video.get("codec"), str) or not video["codec"]
                    or type(video.get("width")) is not int or video["width"] <= 0
                    or type(video.get("height")) is not int or video["height"] <= 0
                ):
                    raise ValueError("generated artifact Sidecar probe values are invalid")
                media_id = uploaded["mediaId"]
                if media_id != expected_media_id:
                    raise ValueError("generated artifact returned an unexpected idempotent media identity")
    
                # The storage reservation commit above intentionally ends the first task
                # lock before the network call. Re-open a serialized transaction after
                # upload and re-read the current task before any asset/SETTLED write.
                db.rollback()
                begin_serialized_write(db)
                current_statement = (
                    select(DBGenerationTask)
                    .where(DBGenerationTask.id == task_id)
                    .execution_options(populate_existing=True)
                )
                if app_engine.dialect.name != "sqlite":
                    current_statement = current_statement.with_for_update()
                task = db.execute(current_statement).scalar_one_or_none()
                if task is None:
                    compensate_artifact_storage_or_hold(task_tenant_id)
                    db.commit()
                    raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})
    
                # A concurrent duplicate intake may have completed while this request
                # was uploading. Reconcile against the durable asset + receipt before
                # treating the now-terminal task as a failed late artifact.
                if task.asset_version_id is not None:
                    completion = db.execute(
                        select(DBGenerationEvent).where(
                            DBGenerationEvent.generation_task_id == task.id,
                            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
                            DBGenerationEvent.actor_id == x_worker_id,
                        )
                    ).scalar_one_or_none()
                    existing_asset = db.get(DBAssetVersion, task.asset_version_id)
                    same_ingest = (
                        task.provider_artifact_id == providerArtifactId
                        and completion is not None
                        and existing_asset is not None
                        and existing_asset.tenant_id == task.tenant_id
                        and existing_asset.project_id == task.project_id
                        and existing_asset.media_id == expected_media_id
                        and existing_asset.sha256 == digest.hexdigest()
                    )
                    if same_ingest:
                        persist_duplicate_storage_reservation_release()
                        return generation_task_response(
                            db, task, rights=generation_rights(db, task), include_history=True
                        )
    
                now = utc_now()
                invalid_code = None
                invalid_message = None
                # Serialize the final artifact commit with the Owner emergency stop.
                # The kill-switch can be activated while the sidecar upload is in flight.
                final_circuit = generation_circuit(db, task.tenant_id, create=True, for_update=True)
                if final_circuit is not None and final_circuit.state == "DISABLED":
                    invalid_code = "PROVIDER_EMERGENCY_STOPPED"
                    invalid_message = "紧急停机期间迟到产物已隔离"
                elif task.cancel_requested_at is not None or task.status == "CANCELED":
                    invalid_code = "TASK_CANCELED"
                    invalid_message = "任务已取消，迟到产物已隔离"
                elif task.lease_owner != x_worker_id or task.lease_expires_at is None or _as_utc(task.lease_expires_at) < now:
                    invalid_code = "LEASE_LOST"
                    invalid_message = "上传期间工作节点已失去任务租约"
                elif x_worker_lease_epoch is None or x_worker_lease_epoch != task.lease_epoch:
                    invalid_code = "LEASE_EPOCH_MISMATCH"
                    invalid_message = "上传期间任务租约版本已更新"
                elif task.provider_artifact_id and task.provider_artifact_id != providerArtifactId:
                    invalid_code = "ARTIFACT_ID_MISMATCH"
                    invalid_message = "上传期间任务关联的上游产物编号已改变"
                elif task.status not in {"RUNNING", "INGESTING"}:
                    invalid_code = "ARTIFACT_INTAKE_NOT_ALLOWED"
                    invalid_message = "上传期间任务状态已改变"
    
                # G72: a pre-upload Owner check is not authorization for the
                # final asset/SETTLED commit. The storage-reservation commit
                # releases that earlier transaction while FAKE Sidecar runs.
                # Lock the original actor and project rows in this final
                # transaction, using the same row locks as account deactivation,
                # role updates and project reassignment. An earlier revocation
                # wins and rejects the artifact; a later revocation must wait
                # until this transaction commits. This does not grant admins
                # delegation or change tenant/project policy.
                if invalid_code is None and task.provider == "deterministic-fake-talking-head":
                    final_owner_query = select(DBUser.id).where(
                        DBUser.id == task.requested_by,
                        DBUser.tenant_id == task.tenant_id,
                        DBUser.role == "owner",
                        DBUser.is_active.is_(True),
                    )
                    final_project_query = select(DBProject.id).where(
                        DBProject.id == task.project_id,
                        DBProject.tenant_id == task.tenant_id,
                        DBProject.owner_id == task.requested_by,
                    )
                    if app_engine.dialect.name == "postgresql":
                        final_owner_query = final_owner_query.with_for_update()
                        final_project_query = final_project_query.with_for_update()
                    final_owner = db.execute(final_owner_query).scalar_one_or_none()
                    final_project = db.execute(final_project_query).scalar_one_or_none()
                    if final_owner is None or final_project is None:
                        invalid_code = "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED"
                        invalid_message = "上传期间原发起人授权或项目归属已失效，模拟产物隔离"

                # The initial FAKE talking-head rights check precedes a network
                # upload and its committed storage reservation. A REMIX revocation
                # may commit while that upload is in flight. Re-read the persisted
                # source and rights in the final serialized artifact transaction:
                # never ingest/settle bytes against rights already revoked.
                if invalid_code is None and task.provider == "deterministic-fake-talking-head":
                    current_request = task.request_json
                    if not isinstance(current_request, dict):
                        invalid_code = "FAKE_TALKING_HEAD_REQUEST_INVALID"
                        invalid_message = "上传期间口播任务请求结构异常，模拟产物隔离"
                    else:
                        current_source_id = current_request.get("imageAssetId")
                        current_source_hash = current_request.get("sourceAssetSha256")
                        if (
                            not isinstance(current_source_id, str) or not current_source_id
                            or not isinstance(current_source_hash, str) or not current_source_hash
                        ):
                            invalid_code = "FAKE_TALKING_HEAD_REQUEST_INVALID"
                            invalid_message = "上传期间口播素材字段异常，模拟产物隔离"
                        elif (
                            current_request.get("fakeTaskType") != "talking-head"
                            or current_request.get("simulatedOutput") is not True
                            or current_request.get("publishable") is not False
                            or current_request.get("confirmSyntheticTestOnly") is not True
                            or type(current_request.get("outputCount")) is not int
                            or current_request["outputCount"] != 1
                            or current_request.get("inputAssetVersionIds") != [current_source_id]
                            or not fake_talking_head_creation_evidence_matches(db, task, current_request)
                        ):
                            # Sidecar upload ends the first DB transaction. Recheck
                            # the complete FAKE-only promise after reacquiring the
                            # final write lock, before committing asset or usage.
                            invalid_code = "FAKE_TALKING_HEAD_REQUEST_INVALID"
                            invalid_message = "上传期间口播FAKE持久合约已变化，模拟产物隔离"
                        else:
                            # G72: hold the same PostgreSQL asset-version lock
                            # used by rights-snapshot writers through asset+SETTLED
                            # commit. A revocation committed first is re-read here;
                            # a later revoke cannot ACK before this commit.
                            source_statement = select(DBAssetVersion).where(
                                DBAssetVersion.id == current_source_id,
                                DBAssetVersion.tenant_id == task.tenant_id,
                                DBAssetVersion.project_id == task.project_id,
                            )
                            if app_engine.dialect.name != "sqlite":
                                source_statement = source_statement.with_for_update()
                            current_source = db.execute(source_statement).scalar_one_or_none()
                            if (
                                current_source is None
                                or current_source.media_type != "image"
                                or current_source.sha256 != current_source_hash
                                or not rights_decision(
                                    latest_rights_snapshot(db, current_source.id, "REMIX"), now
                                )["allowed"]
                            ):
                                invalid_code = "FAKE_TALKING_HEAD_SOURCE_REVOKED"
                                invalid_message = "上传期间口播素材授权或版本失效，模拟产物隔离"
    
                if invalid_code:
                    attempt = current_generation_attempt(db, task)
                    orphan_size = int(metadata.get("sizeBytes") or upload_size) if isinstance(metadata, dict) else upload_size
                    orphan_evidence = {
                        "reasonCode": invalid_code,
                        "workerId": x_worker_id,
                        "leaseEpoch": x_worker_lease_epoch,
                        "reservedStorageBytes": upload_size,
                    }
                    if media_id and uploaded.get("projectId") == task_project_id:
                        orphan_evidence.update({
                            "orphanedMediaId": media_id,
                            "orphanedMediaSha256": digest.hexdigest(),
                            "orphanedMediaSizeBytes": orphan_size,
                            "originalProviderArtifactId": providerArtifactId,
                            "currentTaskProviderArtifactId": task.provider_artifact_id,
                            "originalUpstreamJobId": task.upstream_job_id or "",
                            "orphanedMediaMetadata": metadata if isinstance(metadata, dict) else {},
                        })
                        if invalid_code == "TASK_CANCELED" and task.media_id is None:
                            task.media_id = media_id
                            task.provider_artifact_id = task.provider_artifact_id or providerArtifactId
                            task.updated_at = now
                    # FAKE-only G72: the existing rejected-intake quota decrement
                    # must have a durable, same-transaction receipt referencing
                    # its precise OPENED event. This records an existing release;
                    # it does not authorize new refunds or protected-store actions.
                    if invalid_code == "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED":
                        db.add(DBGenerationEvent(
                            id=str(uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"aether-storage-release:{storage_reservation_event_id}:g72-owner-revoked",
                            )),
                            generation_task_id=task_id,
                            attempt_id=attempt.id if attempt is not None else None,
                            event_type="STORAGE_RESERVATION_RELEASED",
                            from_status=task.status,
                            to_status=task.status,
                            actor_type="SYSTEM",
                            actor_id="api",
                            metadata_json={
                                "reasonCode": "FAKE_TALKING_HEAD_OWNER_AUTH_REVOKED",
                                "storageReservationEventId": storage_reservation_event_id,
                                "workerId": x_worker_id,
                                "leaseEpoch": x_worker_lease_epoch,
                                "providerArtifactId": providerArtifactId,
                                "mediaId": expected_media_id,
                                "sha256": digest.hexdigest(),
                                "reservedStorageBytes": upload_size,
                            },
                            created_at=now,
                        ))
                    compensate_artifact_storage_or_hold(task.tenant_id)
                    add_generation_event(
                        db, task, attempt=attempt, event_type="ARTIFACT_INGEST_FAILED",
                        actor_type="SYSTEM", actor_id="api",
                        from_status=task.status, to_status=task.status,
                        metadata=orphan_evidence, now=now,
                    )
                    db.commit()
                    raise HTTPException(status_code=409, detail={"code": invalid_code, "message": invalid_message})
    
                if not media_id or uploaded.get("projectId") != task.project_id:
                    raise ValueError("generated artifact returned invalid media scope")
                if not isinstance(metadata, dict) or not metadata.get("video") or float(metadata.get("durationSeconds") or 0) <= 0:
                    raise ValueError("generated artifact did not pass media probe")
    
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
                generated_seconds = max(1, int(duration_seconds + 0.999999))
                settled_seconds = db.execute(
                    select(func.coalesce(func.sum(DBGenerationUsageEntry.generated_seconds), 0)).where(
                        DBGenerationUsageEntry.tenant_id == task.tenant_id,
                        DBGenerationUsageEntry.kind.in_(["SETTLED", "ADJUSTED"]),
                        DBGenerationUsageEntry.created_at >= now.replace(
                            day=1, hour=0, minute=0, second=0, microsecond=0
                        ),
                    )
                ).scalar_one()
                seconds_limit = int(
                    (task.capability_snapshot_json.get("quota") or {}).get(
                        "monthlyGeneratedSecondsLimit", 0
                    )
                )
                if seconds_limit < 1 or int(settled_seconds) + generated_seconds > seconds_limit:
                    raise ValueError("generated seconds quota exceeded")
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
                add_usage_entry(
                    db, task=task, attempt=attempt, kind="SETTLED",
                    request_units=0, generated_seconds=generated_seconds,
                    reservation_key=f"generation:{task.id}",
                    config_version_id=task.capability_snapshot_json.get("configVersionId"),
                    now=now,
                )
                record_generation_success(db, task, issuer_id=x_worker_id, now=now)
                add_generation_event(
                    db, task, attempt=attempt, event_type="ARTIFACT_INGESTED",
                    actor_type="WORKER", actor_id=x_worker_id,
                    from_status=previous, to_status="RIGHTS_BLOCKED",
                    metadata={"assetVersionId": asset.id, "sha256": asset.sha256,
                              "sizeBytes": size_bytes, "generatedSeconds": generated_seconds}, now=now,
                )
                db.commit()
            except HTTPException:
                raise
            except Exception as exc:
                db.rollback()
                if duplicate_storage_release_requested:
                    existing_release = db.get(DBGenerationEvent, duplicate_storage_release_event_id)
                    if existing_release is not None:
                        existing_bytes = int(
                            (existing_release.metadata_json or {}).get("reservedStorageBytes") or -1
                        )
                        if (
                            existing_release.generation_task_id != task_id
                            or existing_release.event_type != "STORAGE_RESERVATION_RELEASED"
                            or existing_bytes != upload_size
                        ):
                            raise RuntimeError("duplicate storage release receipt does not match this request")
                        db.rollback()
                        duplicate_statement = (
                            select(DBGenerationTask)
                            .where(DBGenerationTask.id == task_id)
                            .execution_options(populate_existing=True)
                        )
                        if app_engine.dialect.name != "sqlite":
                            duplicate_statement = duplicate_statement.with_for_update()
                        duplicate_task = db.execute(duplicate_statement).scalar_one_or_none()
                        if duplicate_task is None:
                            raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"}) from exc
                        return generation_task_response(
                            db, duplicate_task, rights=generation_rights(db, duplicate_task), include_history=True
                        )
                    db.rollback()
                    raise
                begin_serialized_write(db)
                failed_statement = (
                    select(DBGenerationTask)
                    .where(DBGenerationTask.id == task_id)
                    .execution_options(populate_existing=True)
                )
                if app_engine.dialect.name != "sqlite":
                    failed_statement = failed_statement.with_for_update()
                failed_task = db.execute(failed_statement).scalar_one_or_none()
                if failed_task is None:
                    compensate_artifact_storage_or_hold(task_tenant_id)
                    db.commit()
                    raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"}) from exc
    
                # A commit can succeed while its ACK is lost. Reconcile only against the
                # durable asset, artifact ID, worker receipt and content hash. If the
                # sidecar call itself did not return, this request's storage reservation
                # is duplicate and can be released. If it returned, preserve storage:
                # this may be the original request whose final DB commit ACK was lost.
                if failed_task.asset_version_id is not None:
                    completion = db.execute(
                        select(DBGenerationEvent).where(
                            DBGenerationEvent.generation_task_id == failed_task.id,
                            DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
                            DBGenerationEvent.actor_id == x_worker_id,
                        )
                    ).scalar_one_or_none()
                    existing_asset = db.get(DBAssetVersion, failed_task.asset_version_id)
                    same_ingest = (
                        failed_task.provider_artifact_id == providerArtifactId
                        and completion is not None
                        and existing_asset is not None
                        and existing_asset.tenant_id == failed_task.tenant_id
                        and existing_asset.project_id == failed_task.project_id
                        and existing_asset.media_id == expected_media_id
                        and existing_asset.sha256 == digest.hexdigest()
                    )
                    if same_ingest:
                        if uploaded is None:
                            task = failed_task
                            persist_duplicate_storage_reservation_release()
                        else:
                            db.rollback()
                        return generation_task_response(
                            db, failed_task, rights=generation_rights(db, failed_task), include_history=True
                        )
    
                now = utc_now()
                invalid_code = None
                invalid_message = None
                if failed_task.cancel_requested_at is not None or failed_task.status == "CANCELED":
                    invalid_code = "TASK_CANCELED"
                    invalid_message = "任务已取消，迟到产物已隔离"
                elif failed_task.lease_owner != x_worker_id or failed_task.lease_expires_at is None or _as_utc(failed_task.lease_expires_at) < now:
                    invalid_code = "LEASE_LOST"
                    invalid_message = "上传期间工作节点已失去任务租约"
                elif x_worker_lease_epoch is None or x_worker_lease_epoch != failed_task.lease_epoch:
                    invalid_code = "LEASE_EPOCH_MISMATCH"
                    invalid_message = "上传期间任务租约版本已更新"
                elif failed_task.provider_artifact_id and failed_task.provider_artifact_id != providerArtifactId:
                    invalid_code = "ARTIFACT_ID_MISMATCH"
                    invalid_message = "上传期间任务关联的上游产物编号已改变"
                elif failed_task.status not in {"RUNNING", "INGESTING"}:
                    invalid_code = "ARTIFACT_INTAKE_NOT_ALLOWED"
                    invalid_message = "上传期间任务状态已改变"
    
                if invalid_code:
                    attempt = current_generation_attempt(db, failed_task)
                    failure_metadata = {
                        "reasonCode": invalid_code,
                        "workerId": x_worker_id,
                        "leaseEpoch": x_worker_lease_epoch,
                        "reservedStorageBytes": upload_size,
                    }
                    if uploaded is not None and media_id and uploaded.get("projectId") == task_project_id:
                        failure_metadata.update({
                            "orphanedMediaId": media_id,
                            "orphanedMediaSha256": digest.hexdigest(),
                            "orphanedMediaSizeBytes": int(metadata.get("sizeBytes") or upload_size) if isinstance(metadata, dict) else upload_size,
                            "originalProviderArtifactId": providerArtifactId,
                            "currentTaskProviderArtifactId": failed_task.provider_artifact_id,
                            "originalUpstreamJobId": failed_task.upstream_job_id or "",
                            "orphanedMediaMetadata": metadata if isinstance(metadata, dict) else {},
                        })
                        if invalid_code == "TASK_CANCELED" and failed_task.media_id is None:
                            failed_task.media_id = media_id
                            failed_task.provider_artifact_id = failed_task.provider_artifact_id or providerArtifactId
                            failed_task.updated_at = now
                    compensate_artifact_storage_or_hold(failed_task.tenant_id)
                    add_generation_event(
                        db, failed_task, attempt=attempt, event_type="ARTIFACT_INGEST_FAILED",
                        actor_type="SYSTEM", actor_id="api",
                        from_status=failed_task.status, to_status=failed_task.status,
                        metadata=failure_metadata, now=now,
                    )
                    db.commit()
                    raise HTTPException(status_code=409, detail={"code": invalid_code, "message": invalid_message}) from exc
    
                previous_status = failed_task.status
                failed_attempt = current_generation_attempt(db, failed_task)
                failed_task.status = "FAILED"
                failed_task.error_code = "NON_RETRYABLE_ARTIFACT_VALIDATION_FAILED" if isinstance(exc, ValueError) else "ARTIFACT_INGEST_FAILED"
                failed_task.error_message = "生成产物未通过受治理媒体校验" if isinstance(exc, ValueError) else "生成产物入库失败"
                failed_task.completed_at = now
                failed_task.updated_at = now
                failed_task.lease_owner = None
                failed_task.lease_expires_at = None
                failed_attempt.status = "FAILED"
                failed_attempt.error_code = failed_task.error_code
                failed_attempt.error_message = failed_task.error_message
                failed_attempt.completed_at = failed_task.completed_at
                failure_metadata = {"compensatedQuotaBytes": upload_size}
                artifact_recovery_hold = bool(failed_task.upstream_job_id and providerArtifactId)
                if artifact_recovery_hold:
                    # The remote job is already known and may be billable. Storage
                    # compensation is separate from generation usage: preserve the
                    # original RESERVED entry. The deterministic Sidecar ID lets the
                    # Owner inspect/recover a committed object even when its upload
                    # acknowledgement was lost and no response body reached the API.
                    failed_attempt.reconciliation_state = "HELD"
                    failed_task.media_id = expected_media_id
                    failure_metadata.update({
                        "originalProviderArtifactId": providerArtifactId,
                        "originalUpstreamJobId": failed_task.upstream_job_id,
                        "reconciliationState": "HELD",
                        "usageReservation": "HELD",
                        "orphanedMediaId": expected_media_id,
                        "orphanedMediaSha256": digest.hexdigest(),
                        "orphanedMediaSizeBytes": upload_size,
                        "orphanedMediaMetadata": {},
                        "artifactIdempotencyKey": artifact_idempotency_key,
                    })
                if uploaded is not None and media_id and uploaded.get("projectId") == task_project_id:
                    failed_task.media_id = media_id
                    failure_metadata.update({
                        "orphanedMediaId": media_id,
                        "orphanedMediaSha256": digest.hexdigest(),
                        "orphanedMediaSizeBytes": int(metadata.get("sizeBytes") or upload_size) if isinstance(metadata, dict) else upload_size,
                        "originalProviderArtifactId": providerArtifactId,
                        "currentTaskProviderArtifactId": failed_task.provider_artifact_id,
                        "originalUpstreamJobId": failed_task.upstream_job_id or "",
                        "orphanedMediaMetadata": metadata if isinstance(metadata, dict) else {},
                    })
                compensate_artifact_storage_or_hold(failed_task.tenant_id)
                add_generation_event(
                    db, failed_task, attempt=failed_attempt, event_type="ARTIFACT_INGEST_FAILED",
                    actor_type="SYSTEM", actor_id="api", from_status=previous_status,
                    to_status="FAILED", metadata=failure_metadata, now=failed_task.completed_at,
                )
                if not artifact_recovery_hold:
                    release_generation_reservation(
                        db, failed_task, failed_attempt,
                        reason_code=failed_task.error_code, now=failed_task.completed_at,
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
        # FAKE-only G70/G71: serialize *normal direct-media* uploads by
        # tenant+project, not by random per-request UUID. The quota COMMIT
        # intentionally precedes remote Sidecar I/O, so an ordinary DB row
        # lock cannot prevent simultaneous uploads against one stale revision.
        # This session-level advisory guard is only an in-flight concurrency
        # fence. A crashed process still needs trusted orphan reconciliation.
        lock_identity = f"direct-media:{context.tenant_id}:{project_id}"
        project_media_lock_key = (
            int.from_bytes(hashlib.sha256(lock_identity.encode("utf-8")).digest()[:8], "big")
            & 0x7FFFFFFFFFFFFFFF
        )
        with postgres_storage_hold_liveness(project_media_lock_key):
            # Read the current revision *after* acquiring the cross-process
            # lock: ORM state loaded before this point could be stale.
            project = project_for_tenant(db, project_id, context)
            if project.revision != expectedRevision:
                raise HTTPException(status_code=409, detail={"code": "CONCURRENCY_CONFLICT", "message": "项目版本冲突，请重新载入后再操作"})
            # G70/G71: an uncertain prior Sidecar dispatch can outlive this
            # process and its session advisory lock. A durable HELD record
            # fences retransmission before a second quota debit or dispatch.
            # No automatic release or replay is authorized by this record.
            unresolved_hold_id = db.execute(
                select(DBDirectMediaUploadHold.id)
                .where(
                    DBDirectMediaUploadHold.tenant_id == context.tenant_id,
                    DBDirectMediaUploadHold.project_id == project_id,
                    DBDirectMediaUploadHold.state == "HELD",
                )
                .limit(1)
            ).scalar_one_or_none()
            if unresolved_hold_id is not None:
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "此前媒体占额结果待可信核对，禁止重复上传或重新占额",
                    },
                )
            if (
                not isinstance(project.materials, list)
                or any(not isinstance(item, dict) for item in project.materials)
            ):
                # G70/G71: do not debit quota or dispatch bytes before discovering
                # corrupt historical catalog JSON. Otherwise the later append
                # raises after remote I/O, leaving an avoidable HELD orphan.
                # This check is read-only and grants no reconciliation authority.
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PROJECT_MATERIALS_INTEGRITY_REVIEW_REQUIRED",
                        "message": "项目现存素材数据格式异常，未发起上传或占额，须人工核对",
                    },
                )
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
                .where(
                    DBTenant.id == context.tenant_id,
                    DBTenant.used_storage_bytes >= 0,
                    DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes,
                )
                .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=utc_now())
            )
            if reservation.rowcount != 1:
                # A negative counter is corrupt evidence, not spare capacity.
                stored_bytes = db.execute(
                    select(DBTenant.used_storage_bytes).where(DBTenant.id == context.tenant_id)
                ).scalar_one_or_none()
                db.rollback()
                if stored_bytes is not None and stored_bytes < 0:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "存储计数器异常，须人工对账后才能重新占额",
                        },
                    )
                raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "团队存储空间已达到配额上限"})
            # Quota UPDATE and durable hold receipt must commit atomically:
            # after SIGKILL a new process can see HELD before re-dispatch.
            now_hold = utc_now()
            direct_media_hold = DBDirectMediaUploadHold(
                id=str(uuid.uuid4()),
                tenant_id=context.tenant_id,
                project_id=project_id,
                requested_by=context.user_id,
                project_revision=expectedRevision,
                source_sha256=digest.hexdigest(),
                size_bytes=upload_size,
                state="HELD",
                media_id=None,
                created_at=now_hold,
                updated_at=now_hold,
            )
            db.add(direct_media_hold)
            try:
                db.commit()
            except Exception as exc:
                # The quota UPDATE may already be durable when the COMMIT
                # acknowledgement is lost. Never issue a speculative refund or
                # call the Sidecar until the original hold is reconciled.
                db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                        "message": "媒体存储预留提交结果不明确，须人工核对额度后再重试",
                    },
                ) from exc
            # Once the asset transaction COMMIT is attempted, its outcome can be
            # uncertain (e.g. lost acknowledgement or failed post-commit refresh).
            # The reservation is already durable; never refund it blindly.
            asset_commit_attempted = False
            sidecar_call_started = False
            try:
                # Once invoked, the remote Sidecar may have committed bytes even if
                # it returns malformed metadata or raises a non-VideoUseError.
                # Never refund this quota hold without independent non-write proof.
                sidecar_call_started = True
                uploaded = created_app.state.video_use.upload_media(project_id, file.filename or "media.mp4", file.content_type, file.file)
                # Video-use is an external process. Reject non-scalar or non-finite
                # probe evidence before persisting a project material or asset.
                # Storage quota was reserved above. After Sidecar dispatch,
                # malformed/untrusted replies retain the quota hold for
                # independent reconciliation, not speculative compensation.
                if not isinstance(uploaded, dict):
                    raise HTTPException(status_code=422, detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的产物结构无效"})
                metadata = uploaded.get("metadata")
                if not isinstance(metadata, dict):
                    raise HTTPException(status_code=422, detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的探测数据无效"})
                probed_size_bytes = metadata.get("sizeBytes")
                if type(probed_size_bytes) is not int or probed_size_bytes <= 0:
                    raise HTTPException(status_code=422, detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的字节数无效"})
                # G70/G71: the storage debit is based on the exact uploaded bytes.
                # A different Sidecar probe size cannot be committed as the asset's
                # size; otherwise one tenant can retain unaccounted storage or be
                # charged for bytes that were not uploaded. Hold quota pending
                # trusted remote-orphan reconciliation, not a guessed refund.
                if probed_size_bytes != upload_size:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "MEDIA_PROBE_SIZE_MISMATCH",
                            "message": "媒体服务返回的字节数与原始上传不一致，禁止登记资产",
                        },
                    )
                raw_duration = metadata.get("durationSeconds")
                if raw_duration is not None and (
                    type(raw_duration) not in (int, float)
                    or not math.isfinite(raw_duration)
                    or raw_duration < 0
                ):
                    raise HTTPException(status_code=422, detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的时长无效"})
                duration_seconds = float(raw_duration or 0)
                suffix = os.path.splitext(file.filename or "")[1].lower()
                image_extensions = {".png", ".jpg", ".jpeg", ".webp"}
                video_probe = metadata.get("video")
                audio_probe = metadata.get("audio")
                # G70/G71: the upload's matching bytes/hash do not certify its
                # media probe. Never persist a video/audio asset from a malformed
                # Sidecar stream descriptor or silently label a trackless video
                # as audio. Since dispatch already happened, the existing outer
                # exception fence preserves HELD for trusted reconciliation.
                if video_probe is not None and (
                    not isinstance(video_probe, dict)
                    or not isinstance(video_probe.get("codec"), str)
                    or not video_probe["codec"].strip()
                    or type(video_probe.get("width")) is not int
                    or video_probe["width"] <= 0
                    or type(video_probe.get("height")) is not int
                    or video_probe["height"] <= 0
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的视频轨信息无效"},
                    )
                if audio_probe is not None and (
                    not isinstance(audio_probe, dict)
                    or not isinstance(audio_probe.get("codec"), str)
                    or not audio_probe["codec"].strip()
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务返回的音频轨信息无效"},
                    )
                if suffix in image_extensions:
                    if (
                        uploaded.get("mediaType") != "image"
                        or not video_probe
                        or audio_probe is not None
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail={"code": "IMAGE_PROBE_INVALID", "message": "图片未通过图像格式与尺寸校验"},
                        )
                    media_type = "image"
                else:
                    if not video_probe and not audio_probe:
                        raise HTTPException(
                            status_code=422,
                            detail={"code": "MEDIA_PROBE_INVALID", "message": "媒体服务未返回有效的音视频轨信息"},
                        )
                    media_type = "video" if video_probe else "audio"
                # G70/G71: a Sidecar mediaType that contradicts the validated
                # stream descriptor must not be registered as an asset. A matching
                # byte hash does not certify a corrupted media classification.
                # Once dispatch began, the outer failure fence retains HELD.
                if uploaded.get("mediaType") != media_type:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "MEDIA_PROBE_TYPE_MISMATCH",
                            "message": "媒体服务返回的媒体分类与实际轨道不一致，禁止登记资产",
                        },
                    )
                # Sidecar upload replies are not tenant-authority evidence. Before
                # adding a durable asset/material, require the returned project
                # identity to match the authenticated tenant-scoped project and
                # constrain mediaId to the Sidecar's safe path-identifier contract.
                # A syntactically valid reply is NOT an independently signed receipt.
                media_id = uploaded.get("mediaId")
                if (
                    uploaded.get("projectId") != project_id
                    or type(media_id) is not str
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", media_id) is None
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "MEDIA_PROBE_IDENTITY_MISMATCH",
                            "message": "媒体服务返回的项目或媒体身份不可信，禁止登记资产",
                        },
                    )
                # G70/G71: the Sidecar ACK and matching byte length alone
                # do not prove that the persisted object has the source bytes.
                # Same-Sidecar inspection is NOT an independent signed issuer.
                inspected = created_app.state.video_use.inspect_media(project_id, media_id)
                if (
                    not isinstance(inspected, dict)
                    or inspected.get("projectId") != project_id
                    or inspected.get("mediaId") != media_id
                    or type(inspected.get("sizeBytes")) is not int
                    or inspected["sizeBytes"] != upload_size
                    or type(inspected.get("sha256")) is not str
                    or inspected["sha256"].lower() != digest.hexdigest()
                ):
                    # Post-dispatch UNKNOWN remains HELD; no speculative refund.
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                                "message": "媒体存储摘要或身份与上传不符，须可信对账"},
                    )
                fallback_content_type = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".webp": "image/webp",
                }.get(suffix, "video/mp4" if media_type == "video" else "audio/mpeg")
                # G70/G71: the caller-controlled multipart MIME type is not a
                # Sidecar-proven media classification. Never persist HTML/SVG
                # as an authenticated video/image asset solely from that header.
                # Keep acceptable video/audio MIME types; derive a safe fallback
                # from the verified track class and known image file extension.
                declared_mime = (file.content_type or "").split(";", 1)[0].strip().lower()
                safe_media_mime = (
                    declared_mime
                    if media_type != "image"
                    and declared_mime.startswith(f"{media_type}/")
                    and re.fullmatch(r"[a-z0-9][a-z0-9_.+-]*/[a-z0-9][a-z0-9_.+-]*", declared_mime)
                    else fallback_content_type
                )
                material = {
                    "id": media_id, "name": uploaded.get("fileName") or file.filename or media_id,
                    "url": f"/api/video-use/media/{project_id}/{media_id}", "type": media_type,
                    "contentType": safe_media_mime,
                    "duration": None if media_type == "image" else {
                        "value": max(1, round(duration_seconds * 24_000)), "timescale": 24_000,
                    },
                    "sizeBytes": probed_size_bytes,
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
                # The same asset COMMIT transitions the matching durable hold.
                # A lost COMMIT ACK may mean either HELD or SETTLED; both
                # outcomes must be read back, never guessed or refunded.
                direct_media_hold.state = "SETTLED"
                direct_media_hold.media_id = media_id
                direct_media_hold.updated_at = utc_now()
                asset_commit_attempted = True
                db.commit()
                db.refresh(asset_version)
            except Exception as exc:
                db.rollback()
                if asset_commit_attempted:
                    # The asset commit may already be durable. Compensating its
                    # storage reservation here could undercount a real asset.
                    # Return UNKNOWN for independent reconciliation; do not guess
                    # whether the asset, material and quota are fully committed.
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "媒体资产提交结果不明确，须人工核对产物和额度，禁止自动退回预留",
                        },
                    ) from exc
                if sidecar_call_started:
                    # Any error after dispatch (including malformed Sidecar probe or
                    # a generic transport exception) may leave durable remote bytes.
                    # This is a fail-closed hold, NOT a trusted reconciliation ACK.
                    logger.exception("video-use media dispatch/probe outcome uncertain")
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "媒体上传或回执不可信，保留原始存储预留供人工核对，禁止自动退款或重试",
                        },
                    ) from exc
                compensation = db.execute(
                    update(DBTenant)
                    .where(
                        DBTenant.id == context.tenant_id,
                        DBTenant.used_storage_bytes >= upload_size,
                    )
                    .values(used_storage_bytes=DBTenant.used_storage_bytes - upload_size)
                )
                if compensation.rowcount != 1:
                    # Never certify a refund when the reserved bytes are missing.
                    # Preserve the inconsistent counter for trusted reconciliation.
                    db.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ARTIFACT_STORAGE_HOLD_RECONCILIATION_REQUIRED",
                            "message": "存储预留与额度不一致，需要人工核对",
                        },
                    ) from exc
                db.commit()
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
        # G72: serialize every source-rights snapshot writer with FAKE-only
        # Worker final intake by holding the same asset-version row lock until
        # commit. This prevents an Owner revoke ACK from racing past the
        # Worker's last REMIX check while it still settles stale ALLOWED usage.
        asset_for_tenant(db, project_id, asset_version_id, context, for_update=True)
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
        asset = asset_for_tenant(db, project_id, asset_version_id, context)
        snapshot = latest_rights_snapshot(db, asset_version_id, purpose)
        generated_rights = (
            generated_asset_export_rights(db, asset)
            if purpose in (None, "EXPORT")
            else None
        )
        decision = (
            generated_rights
            if generated_rights is not None
            else rights_decision(snapshot, utc_now())
        )
        return {
            "assetVersionId": asset_version_id,
            **decision,
            "snapshot": rights_snapshot_response(snapshot) if snapshot else None,
        }

    def proxy_video_use_stream(
        path: str,
        request: Request,
        media_type: str = "application/octet-stream",
        prefer_catalog_mime: bool = False,
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

        if prefer_catalog_mime:
            # Only authenticated catalogued assets use their persisted,
            # track-validated MIME rather than an upstream HTTP header.
            response_headers["x-content-type-options"] = "nosniff"
        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=media_type if prefer_catalog_mime else upstream.headers.get("content-type", media_type),
            headers=response_headers,
        )

    @created_app.get("/video-use/media/{project_id}/{media_id}")
    def stream_project_media(project_id: str, media_id: str, request: Request, context: AuthContext = Depends(context_dependency), db: Session = Depends(db_dependency)):
        project = project_for_tenant(db, project_id, context)
        material = next((item for item in project.materials if item.get("id") == media_id), None)
        if material is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND", "message": "未找到该媒体文件"})

        # Generated outputs are downloadable only while their latest EXPORT
        # evidence is valid. A RIGHTS_BLOCKED task must not become readable
        # merely because recovery added the orphan to project.materials.
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
        if asset is not None:
            generated_rights = generated_asset_export_rights(db, asset)
            if generated_rights is not None and not generated_rights.get("allowed"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": generated_rights.get("code", "RIGHTS_MISSING"),
                        "message": "该生成资产当前缺少可信 EXPORT 权利证据，禁止下载",
                    },
                )

        media_type = (asset.content_type if asset is not None else None) or material.get("contentType") or (
            "video/mp4" if material.get("type") == "video" else "audio/mpeg"
        )
        return proxy_video_use_stream(
            f"/media/{project_id}/{media_id}", request, media_type,
            prefer_catalog_mime=asset is not None,
        )

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

        # G70/G71: adoption checks EXPORT rights, but the direct artifact route
        # is also a download path. Reuse the existing per-source EXPORT rules
        # on every request, including after a source's rights are revoked.
        # A completed render is NOT independently trusted export authorization.
        render_payload = task.render_payload if isinstance(task.render_payload, dict) else {}
        timeline = render_payload.get("canonicalTimeline")
        tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
        source_ids: set[str] = set()
        malformed = not isinstance(tracks, list) or not tracks
        if not malformed:
            for track in tracks:
                clips = track.get("clips") if isinstance(track, dict) else None
                if not isinstance(clips, list):
                    malformed = True
                    break
                for clip in clips:
                    media_id = clip.get("materialId") if isinstance(clip, dict) else None
                    # Canonical subtitles may be text-only with materialId="".
                    # They are not media sources. The actual video/audio clips
                    # still need current EXPORT evidence; never drop a present
                    # subtitle materialId from the rights scan.
                    if isinstance(track, dict) and track.get("type") == "subtitle" and media_id == "":
                        if not isinstance(clip.get("text"), str) or not clip["text"].strip():
                            malformed = True
                            break
                        continue
                    if not isinstance(media_id, str) or not media_id:
                        malformed = True
                        break
                    source_ids.add(media_id)
                if malformed:
                    break

        failures = []
        if malformed or not source_ids:
            failures.append({"code": "RENDER_SOURCE_PROVENANCE_MISSING"})
        else:
            for media_id in sorted(source_ids):
                asset = db.execute(
                    select(DBAssetVersion)
                    .where(
                        DBAssetVersion.project_id == task.project_id,
                        DBAssetVersion.tenant_id == context.tenant_id,
                        DBAssetVersion.media_id == media_id,
                    )
                    .order_by(DBAssetVersion.version_no.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if asset is None:
                    failures.append({"mediaId": media_id, "code": "ASSET_VERSION_MISSING"})
                    continue
                decision = generated_asset_export_rights(db, asset)
                if decision is None:
                    decision = rights_decision(
                        latest_rights_snapshot(db, asset.id, "EXPORT"), utc_now()
                    )
                if not decision.get("allowed"):
                    failures.append({
                        "mediaId": media_id,
                        "assetVersionId": asset.id,
                        "code": decision.get("code", "RIGHTS_MISSING"),
                    })

        if failures:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "RIGHTS_CHECK_FAILED",
                    "message": "渲染产物下载需要当前有效的原始资产 EXPORT 权利证据",
                    "failures": failures,
                },
            )
        return proxy_video_use_stream(f"/jobs/{task.upstream_job_id}/artifact", request, "video/mp4")

    @created_app.post("/internal/render-tasks/recover")
    def recover_tasks(
        x_worker_token: str | None = Header(default=None),
        x_worker_id: str = Header(default="worker"),
        db: Session = Depends(db_dependency),
    ):
        require_internal_token(x_worker_token)
        # Recovery and claim/update must serialize on the same task row.
        # A plain SELECT can race with a renewed Worker lease and incorrectly
        # quarantine a live task or erase the new lease owner.
        begin_serialized_write(db)
        now = utc_now()
        stale_query = (
            select(DBRenderTask)
            .where(
                DBRenderTask.status.in_(database_status_values("RUNNING")),
                DBRenderTask.lease_expires_at < now,
            )
            .order_by(DBRenderTask.created_at.asc())
        )
        if app_engine.dialect.name != "sqlite":
            stale_query = stale_query.with_for_update(skip_locked=True)
        stale = list(db.execute(stale_query).scalars())
        recovered = []
        for task in stale:
            # A Worker may have submitted the render before losing the upstream
            # acknowledgement. Without a durable upstream ID, a new dispatch
            # could duplicate a charged render. Preserve UNKNOWN for Owner review.
            if task.upstream_job_id:
                task.status = "RUNNING"
                task.message = "已记录上游任务，仅允许继续查询，不得重新提交"
            else:
                task.status = "UNKNOWN"
                task.message = "Worker租约到期且缺少可信上游任务回执；须人工核验，禁止自动重派"
                task.completed_at = now
            task.lease_owner = None
            task.lease_expires_at = None
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
                # Existing QUEUED rows that were already leased may represent a
                # lost submission ACK. Only never-leased work may first submit;
                # RUNNING work may be resumed solely with a durable upstream ID.
                or_(
                    and_(
                        DBRenderTask.status.in_(database_status_values("QUEUED")),
                        DBRenderTask.attempts == 0,
                    ),
                    and_(
                        DBRenderTask.status.in_(database_status_values("RUNNING")),
                        DBRenderTask.upstream_job_id.is_not(None),
                    ),
                ),
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
        # G70/G71: lock the row before validating a Worker heartbeat/update.
        # Otherwise recovery or a new claim can race with a stale completion.
        begin_serialized_write(db)
        task_query = select(DBRenderTask).where(DBRenderTask.id == task_id)
        if app_engine.dialect.name != "sqlite":
            task_query = task_query.with_for_update()
        task = db.execute(task_query).scalar_one_or_none()
        if task is None:
            raise HTTPException(status_code=404, detail={"code": "TASK_NOT_FOUND", "message": "未找到该任务"})
        if task.lease_owner != x_worker_id:
            raise HTTPException(status_code=409, detail={"code": "LEASE_LOST", "message": "当前工作节点已失去该任务租约"})
        now = utc_now()
        # Use a SQL predicate for expiry: SQLite may deserialize datetimes
        # without tzinfo, while the PostgreSQL column is timezone-aware.
        live_lease = db.execute(
            select(DBRenderTask.id).where(
                DBRenderTask.id == task_id,
                DBRenderTask.lease_expires_at > now,
            )
        ).scalar_one_or_none()
        if canonical_task_status(task.status) != "RUNNING" or live_lease is None:
            raise HTTPException(status_code=409, detail={"code": "LEASE_EXPIRED", "message": "任务已结束或租约已过期，禁止迟到回写"})
        # Once a Sidecar ID has been accepted, a later callback cannot replace
        # it with a different job (which could attach another task's output).
        if req.upstreamJobId:
            if task.upstream_job_id and task.upstream_job_id != req.upstreamJobId:
                raise HTTPException(status_code=409, detail={"code": "UPSTREAM_JOB_ID_CONFLICT", "message": "上游任务身份已绑定，禁止改写"})
            task.upstream_job_id = req.upstreamJobId
        task.progress = req.progress
        task.message = req.message
        task.error = req.error
        # A retryable Worker failure is not evidence that Sidecar rejected the
        # original dispatch. Never discard a prior upstream ID or auto-requeue.
        if req.status == "FAILED" and req.retryable:
            task.status = "UNKNOWN"
            task.message = "渲染失败回执不确定；须人工核验，禁止自动重派"
        else:
            task.status = req.status
        if task.status in {"SUCCEEDED", "FAILED", "CANCELED", "PARTIAL", "UNKNOWN"}:
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
        project = project_for_tenant(db, project_id, context)
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
            decision = generated_asset_export_rights(db, asset)
            if decision is None:
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

        if created_app.state.governance_gates:
            timeline_digest = sha256_json(project.timeline or {})
            rights_evidence_digest = sha256_json(
                {
                    "mediaIds": sorted(media_ids),
                    "purpose": "EXPORT",
                    "status": "ALLOWED",
                }
            )
            enforce_release_gates(
                release_decision=req.releaseDecision,
                quality_findings=req.qualityFindings,
                parity_preview=req.renderParityPreview,
                parity_final=req.renderParityFinal,
                parity_policy_version=req.renderParityPolicyVersion,
                rule_pack=req.rulePack,
                rule_content=req.ruleContent,
                timeline_version=project.revision,
                timeline_digest=timeline_digest,
                preview_evidence_ref=candidate.artifact_ref,
                rights_evidence_digest=rights_evidence_digest,
                evaluation_time=utc_now(),
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
