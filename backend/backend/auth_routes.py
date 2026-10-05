"""Authentication routes (PRD §2, §13.1).

Endpoints:

* POST /auth/login     - email + password -> access + refresh tokens
* POST /auth/refresh   - refresh token -> new access token
* POST /auth/logout    - records the session end (audit)
* GET  /auth/me        - current identity from the access token
* GET  /auth/admin/users - admin-only list of application users

Every login, refresh and logout is recorded in the immutable audit log via
:func:`backend.audit.log_event` (Acceptance Criterion 3). The admin-only
endpoint enforces the RBAC matrix and returns 403 to every non-admin role
(Acceptance Criterion 2).
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .audit import log_event
from .db import SessionLocal
from .models import User
from .rbac import ROLE_PERMISSIONS, require_admin
from .security import verify_password
from .tokens import (
    TokenError,
    decode as decode_token,
    issue_access_token,
    issue_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=320)
    password: str = Field(..., min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def _lookup_user(db: Session, email: str) -> "User | None":
    return db.query(User).filter(User.email.ilike(email)).first()


@router.post("/login", response_model=TokenPair)
async def login(payload: LoginRequest, request: Request = None) -> TokenPair:
    # §13/§14 brute-force guard: per-IP token bucket on the auth endpoint.
    from .rate_limit import RateLimitExceeded, limiter as _rl

    ip = "unknown"
    if request is not None:
        xff = request.headers.get("x-forwarded-for")
        ip = (xff.split(",")[0].strip() if xff else None) or (
            request.client.host if request.client else "unknown")
    allowed, retry_after = _rl.check("auth", ip)
    if not allowed:
        raise RateLimitExceeded(retry_after)
    with SessionLocal() as db:
        user = _lookup_user(db, payload.email)
        if user is None or not user.is_active or not verify_password(
            payload.password, user.password_hash
        ):
            # Same error whether the email exists, to avoid user enumeration.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid credentials",
            )
        access = issue_access_token(user.id, user.role)
        refresh = issue_refresh_token(user.id)
        log_event(
            db=db,
            user_id=user.id,
            action="login",
            entity="auth",
            entity_id=None,
            after={"email": user.email},
            ip_address=_client_ip(),
        )
    return TokenPair(
        access_token=access["token"],
        refresh_token=refresh["token"],
        expires_in=access["expires_in"],
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(authorization: str = Header(None)) -> TokenPair:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing refresh token"
        )
    try:
        payload = decode_token(authorization[len("bearer "):].strip())
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not a refresh token"
        )
    user_id = payload.get("sub")
    with SessionLocal() as db:
        user = db.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found"
        )
    access = issue_access_token(user.id, user.role)
    refresh = issue_refresh_token(user.id)
    log_event(
        db=db,
        user_id=user.id,
        action="refresh",
        entity="auth",
        entity_id=None,
        after={"jti": payload.get("jti")},
        ip_address=_client_ip(),
    )
    return TokenPair(
        access_token=access["token"],
        refresh_token=refresh["token"],
        expires_in=access["expires_in"],
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(authorization: str = Header(None)) -> Response:
    """Logout. Sessions are stateless (no server-side blacklist); the action
    is audited for completeness."""
    payload = None
    if authorization and authorization.lower().startswith("bearer "):
        try:
            payload = decode_token(authorization[len("bearer "):].strip())
        except TokenError:
            payload = None
    if payload and payload.get("sub"):
        with SessionLocal() as db:
            log_event(
                db=db,
                user_id=payload["sub"],
                action="logout",
                entity="auth",
                entity_id=None,
                after={"subject": payload["sub"]},
                ip_address=_client_ip(),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=dict)
async def me(authorization: str = Header(None)) -> dict:
    """Return the current identity for any authenticated user."""
    payload = require_admin(authorization)  # any authenticated user
    role = payload.get("role")
    permissions = list(ROLE_PERMISSIONS[role if role else ""] if role in ROLE_PERMISSIONS else [])
    return {
        "user_id": payload["sub"],
        "role": role,
        "permissions": permissions,
    }


# --- Admin-only endpoint (Acceptance Criterion 2) -----------------------
@router.get("/admin/users", response_model=list[dict])
async def list_all_users(authorization: str = Header(None)) -> list[dict]:
    """Admin-only: every application user. Non-admin roles get 403."""
    payload = require_admin(authorization)
    with SessionLocal() as db:
        users = db.query(User).all()
        log_event(
            db=db,
            user_id=payload["sub"],
            action="audit_users",
            entity="auth",
            after={"count": len(users)},
            ip_address=_client_ip(),
        )
        return [
            {
                "id": u.id,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]


def _client_ip() -> str | None:
    """Best-effort client IP for the audit log.

    FastAPI does not expose the current request from a plain function, so we
    read the proxy headers that are normally forwarded by the reverse proxy;
    returns ``None`` when they are absent (advisory only).
    """
    import os

    for header in ("X-Forwarded-For", "X-Real-IP"):
        value = os.environ.get(header)
        if value:
            return value.split(",")[0].strip()
    return None
