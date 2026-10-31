from __future__ import annotations

import asyncio
import datetime
import hashlib
import hmac
import json
import logging
import math
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
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
    try:
        return (
            bool(policy.get("enabledIntent"))
            and int(policy.get("concurrentTaskLimit") or 0) == 1
            and int(policy.get("monthlyRequestLimit") or 0) == 1
            and 1 <= int(policy.get("monthlyGeneratedSecondsLimit") or 0) <= 10
            and 1 <= int(policy.get("maxClipDurationSeconds") or 0) <= 10
            and int(policy.get("maxOutputs") or 0) == 1
            and policy.get("artifactPathPrefixes") == ["/tasks/"]
        )
    except (TypeError, ValueError):
        return False


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
        policy = config.policy_json if config is not None else DEFAULT_FAKE_POLICY
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
            elif not bool(config.policy_json.get("enabledIntent")):
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
                elif not private_canary_policy_valid(config.policy_json):
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
                "enabledIntent": bool(config and config.policy_json.get("enabledIntent")),
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

    def validate_fake_talking_head_request(
        db: Session,
        project: DBProject,
        req: TalkingHeadFakeGenerationRequest,
        context: AuthContext,
    ) -> tuple[dict, DBAssetVersion]:
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
        policy = task.capability_snapshot_json.get("providerPolicy") or {}
        window_seconds = max(10, int(policy.get("failureWindow", 300)))
        threshold = max(1, int(policy.get("failureThreshold", 5)))
        cooldown_seconds = max(10, int(policy.get("cooldownSeconds", 60)))
        circuit = generation_circuit(db, task.tenant_id, create=True, for_update=True)
        if circuit.state == "DISABLED":
            return
        cutoff = now - datetime.timedelta(seconds=window_seconds)
        timestamps: list[datetime.datetime] = []
        for raw in circuit.failure_timestamps_json or []:
            try:
                parsed = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
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
        project_for_tenant(db, project_id, context)
        task = generation_task_for_tenant(db, project_id, task_id, context)
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
        try:
            expected_size = int(evidence.get("orphanedMediaSizeBytes") or 0)
        except (TypeError, ValueError):
            expected_size = 0
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

        actual_sha256 = str(inspected.get("sha256") or "").lower()
        try:
            actual_size = int(inspected.get("sizeBytes") or 0)
        except (TypeError, ValueError):
            actual_size = 0
        probe_metadata = inspected.get("metadata")
        video_metadata = (
            probe_metadata.get("video")
            if isinstance(probe_metadata, dict)
            else None
        )
        try:
            duration_seconds = float(
                probe_metadata.get("durationSeconds") or 0
            ) if isinstance(probe_metadata, dict) else 0
        except (TypeError, ValueError):
            duration_seconds = 0
        provider_policy = (
            task.capability_snapshot_json.get("providerPolicy") or {}
            if isinstance(task.capability_snapshot_json, dict)
            else {}
        )
        try:
            policy_max_bytes = int(provider_policy.get("maxArtifactBytes") or max_upload_bytes)
        except (TypeError, ValueError):
            policy_max_bytes = max_upload_bytes

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
        if (
            not isinstance(video_metadata, dict)
            or int(video_metadata.get("width") or 0) <= 0
            or int(video_metadata.get("height") or 0) <= 0
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
        existing_material = next(
            (
                item for item in materials
                if isinstance(item, dict) and item.get("id") == orphan_media_id
            ),
            None,
        )
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
        if existing_material is not None:
            if (
                existing_material.get("url") != material["url"]
                or existing_material.get("type") != "video"
                or int(existing_material.get("sizeBytes") or 0) != actual_size
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ORPHAN_MEDIA_PROJECT_MATERIAL_CONFLICT",
                        "message": "项目中已存在同 ID 但元数据不一致的素材",
                    },
                )
        else:
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
            candidate_mode = candidate.capability_snapshot_json.get("mode")
            if candidate_mode != created_app.state.generation_provider_mode:
                continue
            if candidate.provider == "deterministic-fake-talking-head":
                # Recheck the immutable image version and latest REMIX rights
                # when a Worker claims a FAKE-only task; NEVER allow a real mode.
                stored = candidate.request_json or {}
                if candidate_mode != "deterministic-fake" or stored.get("fakeTaskType") != "talking-head":
                    continue
                source = db.execute(select(DBAssetVersion).where(
                    DBAssetVersion.id == stored.get("imageAssetId"),
                    DBAssetVersion.project_id == candidate.project_id,
                    DBAssetVersion.tenant_id == candidate.tenant_id,
                )).scalar_one_or_none()
                if (
                    source is None or source.media_type != "image"
                    or source.sha256 != stored.get("sourceAssetSha256")
                    or not rights_decision(latest_rights_snapshot(db, source.id, "REMIX"), now)["allowed"]
                ):
                    continue
            candidate_circuit = generation_circuit(db, candidate.tenant_id, create=True, for_update=True)
            if candidate_circuit is not None:
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
            if candidate_mode == "moneyprinter":
                config = published_generation_config(db, candidate.tenant_id)
                if (
                    config is None
                    or config.id != candidate.capability_snapshot_json.get("configVersionId")
                    or config.policy_hash != candidate.capability_snapshot_json.get("policyHash")
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
            source_id = (task.request_json or {}).get("imageAssetId")
            source = db.execute(select(DBAssetVersion).where(
                DBAssetVersion.id == source_id,
                DBAssetVersion.tenant_id == task.tenant_id,
                DBAssetVersion.project_id == task.project_id,
            )).scalar_one_or_none()
            if (
                source is None or source.media_type != "image"
                or source.sha256 != (task.request_json or {}).get("sourceAssetSha256")
                or not rights_decision(latest_rights_snapshot(db, source.id, "REMIX"), utc_now())["allowed"]
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "FAKE_TALKING_HEAD_SOURCE_REVOKED", "message": "输入图片授权或版本已失效，模拟产物隔离"},
                )
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
        reservation = db.execute(
            update(DBTenant)
            .where(DBTenant.id == task_tenant_id, DBTenant.used_storage_bytes + upload_size <= DBTenant.storage_quota_bytes)
            .values(used_storage_bytes=DBTenant.used_storage_bytes + upload_size, updated_at=now)
        )
        if reservation.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail={"code": "STORAGE_QUOTA_EXCEEDED", "message": "团队存储空间已达到配额上限"})
        db.commit()

        uploaded = None
        media_id = None
        metadata = {}
        try:
            uploaded = created_app.state.video_use.upload_media(
                task_project_id, f"generation-{task_id}.mp4", "video/mp4", file.file
            )
            metadata = uploaded.get("metadata") or {}
            media_id = str(uploaded.get("mediaId") or "")

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
                db.execute(
                    update(DBTenant)
                    .where(DBTenant.id == task_tenant_id)
                    .values(used_storage_bytes=case(
                        (DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size),
                        else_=0,
                    ))
                )
                db.commit()
                raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"})

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
                db.execute(
                    update(DBTenant)
                    .where(DBTenant.id == task.tenant_id)
                    .values(used_storage_bytes=case(
                        (DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size),
                        else_=0,
                    ))
                )
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
                db.execute(
                    update(DBTenant)
                    .where(DBTenant.id == task_tenant_id)
                    .values(used_storage_bytes=case(
                        (DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size),
                        else_=0,
                    ))
                )
                db.commit()
                raise HTTPException(status_code=404, detail={"code": "GENERATION_TASK_NOT_FOUND", "message": "未找到该生成任务"}) from exc

            # A commit can succeed while its ACK is lost. Query the durable row and
            # ARTIFACT_INGESTED receipt before attempting any compensation.
            if failed_task.asset_version_id is not None:
                completion = db.execute(
                    select(DBGenerationEvent).where(
                        DBGenerationEvent.generation_task_id == failed_task.id,
                        DBGenerationEvent.event_type == "ARTIFACT_INGESTED",
                        DBGenerationEvent.actor_id == x_worker_id,
                    )
                ).scalar_one_or_none()
                if failed_task.provider_artifact_id == providerArtifactId and completion is not None:
                    db.rollback()
                    return generation_task_response(db, failed_task, rights=generation_rights(db, failed_task), include_history=True)

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
                db.execute(
                    update(DBTenant)
                    .where(DBTenant.id == failed_task.tenant_id)
                    .values(used_storage_bytes=case(
                        (DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size),
                        else_=0,
                    ))
                )
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
                # The remote job is already known and may be billable.  Storage
                # compensation is separate from generation usage: preserve the
                # original RESERVED entry until artifact reconciliation settles it.
                failed_attempt.reconciliation_state = "HELD"
                failure_metadata.update({
                    "originalProviderArtifactId": providerArtifactId,
                    "originalUpstreamJobId": failed_task.upstream_job_id,
                    "reconciliationState": "HELD",
                    "usageReservation": "HELD",
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
            db.execute(
                update(DBTenant)
                .where(DBTenant.id == failed_task.tenant_id)
                .values(used_storage_bytes=case(
                    (DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size),
                    else_=0,
                ))
            )
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
            suffix = os.path.splitext(file.filename or "")[1].lower()
            image_extensions = {".png", ".jpg", ".jpeg", ".webp"}
            if suffix in image_extensions:
                video_probe = metadata.get("video") or {}
                if (
                    uploaded.get("mediaType") != "image"
                    or not video_probe.get("width")
                    or not video_probe.get("height")
                    or metadata.get("audio") is not None
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "IMAGE_PROBE_INVALID", "message": "图片未通过图像格式与尺寸校验"},
                    )
                media_type = "image"
            else:
                media_type = "video" if metadata.get("video") else "audio"
            media_id = uploaded["mediaId"]
            fallback_content_type = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(suffix, "video/mp4" if media_type == "video" else "audio/mpeg")
            material = {
                "id": media_id, "name": uploaded.get("fileName") or file.filename or media_id,
                "url": f"/api/video-use/media/{project_id}/{media_id}", "type": media_type,
                "contentType": file.content_type or fallback_content_type,
                "duration": None if media_type == "image" else {
                    "value": max(1, round(duration_seconds * 24_000)), "timescale": 24_000,
                },
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
            db.execute(update(DBTenant).where(DBTenant.id == context.tenant_id).values(used_storage_bytes=case((DBTenant.used_storage_bytes >= upload_size, DBTenant.used_storage_bytes - upload_size), else_=0)))
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
            generated_task = db.execute(
                select(DBGenerationTask)
                .where(
                    DBGenerationTask.project_id == project_id,
                    DBGenerationTask.tenant_id == context.tenant_id,
                    DBGenerationTask.asset_version_id == asset.id,
                )
                .limit(1)
            ).scalar_one_or_none()
            provenance = (
                asset.probe_json.get("provenance")
                if isinstance(asset.probe_json, dict)
                else None
            )
            if generated_task is not None:
                rights = generation_rights(db, generated_task)
                if not rights.get("allowed"):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": rights.get("code", "RIGHTS_MISSING"),
                            "message": "该生成资产当前缺少有效 EXPORT 权利证据，禁止下载",
                        },
                    )
            elif isinstance(provenance, dict) and provenance.get("generationTaskId"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "GENERATION_TASK_MISSING",
                        "message": "生成资产缺少可核验的原始任务，禁止下载",
                    },
                )

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
