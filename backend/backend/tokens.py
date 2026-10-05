"""JWT access/refresh tokens (PRD §13.1, §2).

Access tokens are short-lived (configurable, default 15 min) and carry the
user id and role. Refresh tokens are long-lived and can be exchanged for a
new access token without a password. Both are signed with the shared
``JWT_SECRET`` using HS256.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from . import config


class TokenError(Exception):
    """Raised when a token cannot be issued or verified."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(payload: dict[str, Any]) -> str:
    headers = {"alg": "HS256"}
    return jwt.encode(payload, config.JWT_SECRET, headers=headers)


def decode(token: str) -> dict[str, Any]:
    """Verify *token* and return its payload, raising :class:`TokenError`."""
    try:
        return jwt.decode(
            token, config.JWT_SECRET, algorithms=["HS256"]
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc


def issue_access_token(user_id: str, role: str) -> dict[str, Any]:
    """Issue a short-lived access token and its expiry timestamp."""
    exp = _now() + timedelta(seconds=config.ACCESS_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": exp,
        "iat": _now(),
        "jti": str(uuid.uuid4()),
    }
    return {"token": _encode(payload), "expires_in": config.ACCESS_TOKEN_TTL_SECONDS}


def issue_refresh_token(user_id: str) -> dict[str, Any]:
    """Issue a long-lived refresh token and its expiry timestamp."""
    exp = _now() + timedelta(seconds=config.REFRESH_TOKEN_TTL_SECONDS)
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": exp,
        "iat": _now(),
        "jti": str(uuid.uuid4()),
    }
    return {"token": _encode(payload), "expires_in": config.REFRESH_TOKEN_TTL_SECONDS}
