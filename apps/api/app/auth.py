from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DBSession, DBTenant, DBUser

SESSION_COOKIE = "aether_session"
ROLES = {"owner", "editor", "viewer"}


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: str
    email: str
    display_name: str
    role: str


def create_session(
    db: Session,
    user: DBUser,
    now,
    lifetime_hours: int,
) -> tuple[DBSession, str]:
    token = secrets.token_urlsafe(48)
    session = DBSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token_hash=token_digest(token),
        expires_at=now + timedelta(hours=lifetime_hours),
        created_at=now,
        last_seen_at=now,
    )
    db.add(session)
    db.commit()
    return session, token


def set_session_cookie(response: Response, token: str, lifetime_hours: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=lifetime_hours * 60 * 60,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def require_auth(request: Request, db: Session, now) -> AuthContext:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Sign in to continue"},
        )
    row = db.execute(
        select(DBSession, DBUser)
        .join(DBUser, DBUser.id == DBSession.user_id)
        .where(
            DBSession.token_hash == token_digest(token),
            DBSession.expires_at > now,
            DBUser.is_active.is_(True),
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "SESSION_EXPIRED", "message": "Your session expired; sign in again"},
        )
    session, user = row
    session.last_seen_at = now
    db.commit()
    return AuthContext(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
    )


def require_roles(context: AuthContext, *allowed: str) -> None:
    if context.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "PERMISSION_DENIED", "message": "Your role cannot perform this action"},
        )


def bootstrap_identity(
    db: Session,
    now,
    password_override: str | None = None,
    email_override: str | None = None,
) -> DBUser | None:
    existing = db.execute(select(DBUser).limit(1)).scalar_one_or_none()
    if existing is not None:
        return existing

    password = password_override if password_override is not None else os.environ.get("AETHER_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not password:
        return None
    email = normalize_email(email_override or os.environ.get("AETHER_BOOTSTRAP_ADMIN_EMAIL", "admin@aether.local"))
    tenant_name = os.environ.get("AETHER_BOOTSTRAP_TENANT_NAME", "Aether Studio")
    tenant = DBTenant(
        id=str(uuid.uuid4()),
        name=tenant_name,
        slug=os.environ.get("AETHER_BOOTSTRAP_TENANT_SLUG", "aether-studio"),
        project_quota=max(1, int(os.environ.get("AETHER_PROJECT_QUOTA", "50"))),
        storage_quota_bytes=max(1, int(os.environ.get("AETHER_STORAGE_QUOTA_BYTES", str(50 * 1024**3)))),
        concurrent_render_quota=max(1, int(os.environ.get("AETHER_CONCURRENT_RENDER_QUOTA", "2"))),
        monthly_render_seconds_quota=max(1, int(os.environ.get("AETHER_MONTHLY_RENDER_SECONDS_QUOTA", "36000"))),
        quota_period=now.strftime("%Y-%m"),
        created_at=now,
        updated_at=now,
    )
    user = DBUser(
        id=str(uuid.uuid4()),
        tenant_id=tenant.id,
        email=email,
        display_name=os.environ.get("AETHER_BOOTSTRAP_ADMIN_NAME", "Aether Owner"),
        password_hash=hash_password(password),
        role="owner",
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.add_all([tenant, user])
    db.commit()
    return user


def public_user(user: DBUser, tenant: DBTenant) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "displayName": user.display_name,
        "role": user.role,
        "tenant": {"id": tenant.id, "name": tenant.name, "slug": tenant.slug},
        "quotas": {
            "projects": tenant.project_quota,
            "storageBytes": tenant.storage_quota_bytes,
            "storageBytesUsed": tenant.used_storage_bytes,
            "concurrentRenders": tenant.concurrent_render_quota,
            "monthlyRenderSeconds": tenant.monthly_render_seconds_quota,
            "monthlyRenderSecondsUsed": tenant.render_seconds_used,
            "period": tenant.quota_period,
        },
    }
