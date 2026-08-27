from __future__ import annotations

import datetime
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    DBAssetVersion,
    DBGenerationAttempt,
    DBGenerationEvent,
    DBGenerationTask,
)


ACTIVE_GENERATION_STATES = {"QUEUED", "SUBMITTING", "RUNNING", "INGESTING"}
TERMINAL_GENERATION_STATES = {
    "RIGHTS_BLOCKED", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN", "PARTIAL"
}
RETRYABLE_GENERATION_STATES = {"FAILED", "PARTIAL"}
WORKER_TRANSITIONS = {
    "SUBMITTING": {"RUNNING", "FAILED", "CANCELED", "UNKNOWN"},
    "RUNNING": {"RUNNING", "INGESTING", "FAILED", "CANCELED", "UNKNOWN", "PARTIAL"},
    "INGESTING": {"INGESTING", "RIGHTS_BLOCKED", "FAILED", "CANCELED", "PARTIAL"},
}


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso_utc(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_capability_snapshot(mode: str, now: datetime.datetime | None = None) -> dict[str, Any]:
    issued_at = now or utc_now()
    enabled = mode == "deterministic-fake"
    snapshot: dict[str, Any] = {
        "provider": "moneyprinter",
        "mode": "deterministic-fake" if enabled else "disabled",
        "enabled": enabled,
        "healthy": enabled,
        "sourceVersion": "im12-im14-deterministic-fake-v1" if enabled else "disabled",
        "issuedAt": iso_utc(issued_at),
        "checkedAt": iso_utc(issued_at),
        "expiresAt": iso_utc(issued_at + datetime.timedelta(minutes=5)),
        "reasonCode": None if enabled else "PROVIDER_DISABLED",
        "videoAspects": ["16:9", "9:16", "1:1"],
        "videoConcatModes": ["random", "sequential"],
        "clipDurationSeconds": {"min": 1, "max": 10},
        "maxOutputs": 1,
        "voices": ["en-US-JennyNeural", "zh-CN-XiaoxiaoNeural"],
    }
    snapshot["snapshotHash"] = sha256_json(snapshot)
    return snapshot


def capability_snapshot_valid(snapshot: dict[str, Any], supplied_hash: str, now: datetime.datetime) -> tuple[bool, str | None]:
    if not snapshot.get("enabled"):
        return False, "PROVIDER_DISABLED"
    if not snapshot.get("healthy"):
        return False, "PROVIDER_UNHEALTHY"
    if supplied_hash != snapshot.get("snapshotHash"):
        return False, "CAPABILITY_SNAPSHOT_UNKNOWN"
    expires_at = datetime.datetime.fromisoformat(str(snapshot["expiresAt"]).replace("Z", "+00:00"))
    if now >= expires_at:
        return False, "CAPABILITY_SNAPSHOT_EXPIRED"
    return True, None


def request_hash(payload: dict[str, Any]) -> str:
    return sha256_json(payload)


def add_event(
    db: Session,
    task: DBGenerationTask,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str,
    from_status: str | None = None,
    to_status: str | None = None,
    attempt: DBGenerationAttempt | None = None,
    metadata: dict[str, Any] | None = None,
    now: datetime.datetime | None = None,
) -> DBGenerationEvent:
    event = DBGenerationEvent(
        id=str(uuid.uuid4()),
        generation_task_id=task.id,
        attempt_id=attempt.id if attempt else None,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_type=actor_type,
        actor_id=actor_id,
        metadata_json=metadata or {},
        created_at=now or utc_now(),
    )
    db.add(event)
    return event


def current_attempt(db: Session, task: DBGenerationTask) -> DBGenerationAttempt:
    return db.execute(
        select(DBGenerationAttempt).where(
            DBGenerationAttempt.generation_task_id == task.id,
            DBGenerationAttempt.attempt_no == task.attempts,
        )
    ).scalar_one()


def safe_error_message(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in ("api_key", "api-key", "token", "secret", "bearer")):
        return "生成任务失败，技术详情已隐藏。"
    return value[:500]


def task_response(
    db: Session,
    task: DBGenerationTask,
    *,
    rights: dict[str, Any] | None = None,
    include_history: bool = False,
) -> dict[str, Any]:
    effective_status = "SUCCEEDED" if task.status == "RIGHTS_BLOCKED" and rights and rights.get("allowed") else task.status
    request = task.request_json or {}
    payload: dict[str, Any] = {
        "taskId": task.id,
        "projectId": task.project_id,
        "provider": task.provider,
        "status": effective_status,
        "storedStatus": task.status,
        "progress": task.progress,
        "message": task.message,
        "attempt": task.attempts,
        "maxAttempts": task.max_attempts,
        "requestSummary": {
            "videoAspect": request.get("videoAspect"),
            "videoClipDuration": request.get("videoClipDuration"),
            "outputCount": request.get("outputCount"),
            "inputAssetVersionIds": request.get("inputAssetVersionIds", []),
            "promptLength": len(str(request.get("videoSubject", ""))),
        },
        "capabilitySnapshotHash": task.capability_snapshot_hash,
        "cancelRequested": task.cancel_requested_at is not None,
        "errorCode": task.error_code,
        "errorMessage": safe_error_message(task.error_message),
        "createdAt": iso_utc(task.created_at),
        "updatedAt": iso_utc(task.updated_at),
        "startedAt": iso_utc(task.started_at),
        "completedAt": iso_utc(task.completed_at),
        "rights": rights or {"allowed": False, "code": "RIGHTS_MISSING"},
        "results": [],
    }
    if task.asset_version_id:
        asset = db.execute(select(DBAssetVersion).where(DBAssetVersion.id == task.asset_version_id)).scalar_one_or_none()
        if asset is not None:
            stored_provenance = (asset.probe_json or {}).get("provenance") or {}
            provenance = {
                key: stored_provenance[key]
                for key in ("provider", "generationTaskId", "attempt", "capabilitySnapshotHash")
                if key in stored_provenance
            }
            payload["results"] = [{
                "assetVersionId": asset.id,
                "mediaId": asset.media_id,
                "checksum": asset.sha256,
                "contentType": asset.content_type,
                "sizeBytes": asset.size_bytes,
                "provenance": provenance,
                "rights": payload["rights"],
            }]
    if include_history:
        attempts = db.execute(
            select(DBGenerationAttempt)
            .where(DBGenerationAttempt.generation_task_id == task.id)
            .order_by(DBGenerationAttempt.attempt_no.asc())
        ).scalars()
        events = db.execute(
            select(DBGenerationEvent)
            .where(DBGenerationEvent.generation_task_id == task.id)
            .order_by(DBGenerationEvent.created_at.asc(), DBGenerationEvent.id.asc())
        ).scalars()
        payload["attempts"] = [{
            "attempt": attempt.attempt_no,
            "status": attempt.status,
            "reconciliationState": attempt.reconciliation_state,
            "errorCode": attempt.error_code,
            "errorMessage": safe_error_message(attempt.error_message),
            "createdAt": iso_utc(attempt.created_at),
            "completedAt": iso_utc(attempt.completed_at),
        } for attempt in attempts]
        payload["events"] = [{
            "id": event.id,
            "type": event.event_type,
            "fromStatus": event.from_status,
            "toStatus": event.to_status,
            "actorType": event.actor_type,
            "createdAt": iso_utc(event.created_at),
            "metadata": event.metadata_json,
        } for event in events]
    return payload
