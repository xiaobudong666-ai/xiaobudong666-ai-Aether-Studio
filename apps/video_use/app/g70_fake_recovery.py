"""Disposable FAKE-only durable upload recovery, never enabled for production.

A stable (projectId, recoveryKey) maps to one physical media object. A byte
digest is mandatory for both upload and readback. No Provider calls, billing,
entitlement claims or protected storage deletion are performed here.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from fastapi import UploadFile

_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


class RecoveryConflict(Exception):
    """Fail-closed scoped media recovery request."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def enabled() -> bool:
    # Both explicit nonproduction gates must be present; never infer from the
    # presence of a key or accept this route in a normal sidecar deployment.
    return (
        os.environ.get("AETHER_FAKE_ONLY_NONPROD", "").lower() == "true"
        and os.environ.get("AETHER_RUNTIME_STAGE", "").lower() == "nonproduction"
    )


def _identity(project_id: str, key: str, sha256: str) -> str:
    if not _KEY.fullmatch(project_id) or not _KEY.fullmatch(key):
        raise RecoveryConflict(422, "Invalid recovery scope")
    if not _SHA.fullmatch(sha256):
        raise RecoveryConflict(422, "Invalid expected SHA256")
    return "g70" + hashlib.sha256(f"{project_id}\x00{key}".encode()).hexdigest()[:32]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _existing(media_dir: Path, media_id: str) -> Path | None:
    # Only final media names are considered. Pending uploads and per-key lock
    # files start with '.' and are deliberately invisible to recovery.
    matches = [p for p in media_dir.glob(f"{media_id}.*") if p.is_file()]
    if len(matches) > 1:
        raise RecoveryConflict(409, "Ambiguous physical media identity")
    return matches[0] if matches else None


def _receipt(
    project_id: str,
    media_id: str,
    path: Path,
    expected_sha256: str,
    media_probe: Callable[[Path], dict[str, Any]],
    *,
    recovered: bool,
) -> dict[str, Any]:
    if _hash_file(path) != expected_sha256:
        raise RecoveryConflict(409, "Physical media SHA256 does not match durable intent")
    try:
        metadata = media_probe(path)
    except Exception as exc:
        raise RecoveryConflict(409, "Physical media did not pass nonproduction probe") from exc
    return {
        "mediaId": media_id,
        "projectId": project_id,
        "fileName": path.name,
        "contentType": "video/mp4",
        "metadata": metadata,
        "sha256": expected_sha256,
        "recovered": recovered,
    }


def lookup(
    media_dir: Path,
    project_id: str,
    recovery_key: str,
    expected_sha256: str,
    media_probe: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    media_id = _identity(project_id, recovery_key, expected_sha256)
    existing = _existing(media_dir, media_id)
    if existing is None:
        raise RecoveryConflict(404, "Original media not found; do not regenerate")
    return _receipt(
        project_id, media_id, existing, expected_sha256, media_probe, recovered=True
    )


async def upload(
    media_dir: Path,
    project_id: str,
    recovery_key: str,
    expected_sha256: str,
    media: UploadFile,
    media_probe: Callable[[Path], dict[str, Any]],
    max_bytes: int,
) -> dict[str, Any]:
    media_id = _identity(project_id, recovery_key, expected_sha256)
    suffix = Path(media.filename or "").suffix.lower()
    if suffix != ".mp4":
        raise RecoveryConflict(415, "Only MP4 is accepted for FAKE generation recovery")
    media_dir.mkdir(parents=True, exist_ok=True)
    lock_path = media_dir / f".{media_id}.lock"
    pending = media_dir / f".{media_id}.{uuid.uuid4().hex}.pending{suffix}"
    final = media_dir / f"{media_id}{suffix}"
    # fcntl is for the Linux/macOS disposable sidecar volume. It protects
    # multiple workers/processes, not just a single Python asyncio event loop.
    try:
        with lock_path.open("a+b") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            try:
                existing = _existing(media_dir, media_id)
                if existing is not None:
                    return _receipt(
                        project_id, media_id, existing, expected_sha256,
                        media_probe, recovered=True,
                    )
                actual = hashlib.sha256()
                size = 0
                with pending.open("wb") as target:
                    while chunk := await media.read(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise RecoveryConflict(413, "Media exceeds FAKE upload limit")
                        actual.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if not size or actual.hexdigest() != expected_sha256:
                    raise RecoveryConflict(409, "Uploaded bytes do not match durable intent")
                os.replace(pending, final)
                # If the process crashes after the rename and before returning
                # the ACK, the deterministic path is recoverable after restart.
                dir_fd = os.open(str(media_dir), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
                return _receipt(
                    project_id, media_id, final, expected_sha256,
                    media_probe, recovered=False,
                )
            finally:
                pending.unlink(missing_ok=True)
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
    finally:
        await media.close()
