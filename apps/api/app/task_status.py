from __future__ import annotations

CANONICAL_TASK_STATES = {
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELED",
    "PARTIAL",
    "UNKNOWN",
}

LEGACY_TO_CANONICAL = {
    "queued": "QUEUED",
    "dispatching": "RUNNING",
    "processing": "RUNNING",
    "completed": "SUCCEEDED",
    "failed": "FAILED",
    "canceled": "CANCELED",
    "cancelled": "CANCELED",
    "partial": "PARTIAL",
    "unknown": "UNKNOWN",
    **{status: status for status in CANONICAL_TASK_STATES},
}

CANONICAL_TO_LEGACY = {
    "QUEUED": "queued",
    "RUNNING": "processing",
    "SUCCEEDED": "completed",
    "FAILED": "failed",
    "CANCELED": "canceled",
    "PARTIAL": "partial",
    "UNKNOWN": "unknown",
}


def canonical_task_status(value: str) -> str:
    normalized = LEGACY_TO_CANONICAL.get(value.strip())
    if normalized is None:
        raise ValueError(f"Unsupported task status: {value}")
    return normalized


def legacy_task_status(value: str) -> str:
    return CANONICAL_TO_LEGACY[canonical_task_status(value)]


def database_status_values(*canonical_states: str) -> set[str]:
    requested = {canonical_task_status(value) for value in canonical_states}
    return {
        alias
        for alias, canonical in LEGACY_TO_CANONICAL.items()
        if canonical in requested
    }
