"""Role-based access control (PRD §2.1, §13.1).

Five canonical roles (``admin``, ``project_manager``, ``translator``,
``revisor``, ``qa_reader``) each map to a fixed set of permissions. Each
user carries a ``role`` that is looked up in :data:`ROLE_PERMISSIONS` at
request time.

The FastAPI dependencies :func:`require_permission` and :func:`require_admin`
are the enforcement points for every protected endpoint: they decode the
access token, resolve the caller's role to its permission set, and raise a
403 when the caller lacks the required permission. This is what makes admin
endpoints return 403 to every other role (Acceptance Criterion 2).
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import Header, HTTPException, status

from . import config
from .tokens import TokenError, decode

# --- Permissions (least-privilege verbs over resource classes) ----------
# These are intentionally granular so each of the five roles maps to a
# meaningful subset. ``admin`` is the only role with write access to users,
# roles, and the local_only / Gateway configuration.
PERMISSION_MANAGE_USERS = "manage_users"
PERMISSION_MANAGE_ROLES = "manage_roles"
PERMISSION_CONFIG_GATEWAY = "config_gateway"
PERMISSION_CONFIG_MODELS = "config_models"
PERMISSION_CONFIG_STORAGE = "config_storage"
PERMISSION_CONFIG_AUTH = "config_auth"
PERMISSION_CONFIG_BACKUP = "config_backup"
PERMISSION_ALL_AUDIT = "all_audit"
PERMISSION_DELETE = "delete"  # cancellazioni

PERMISSION_MANAGE_PROJECTS = "manage_projects"
PERMISSION_EDIT_PROJECT = "edit_project"
PERMISSION_ASSIGN = "assign"
PERMISSION_MANAGE_STRUCTURE = "manage_structure"
PERMISSION_STYLE_GUIDES = "style_guides"
PERMISSION_MANAGE_GLOSSARY = "manage_glossary"
PERMISSION_START_TRANSLATION = "start_translation"
PERMISSION_REVIEW_SEGMENTS = "review_segments"
PERMISSION_APPROVE_SEGMENTS = "approve_segments"
PERMISSION_COMPARE = "compare"
PERMISSION_ANNOTATE_QM = "annotate_qm"
PERMISSION_APPROVE_TERMINOLOGY = "approve_terminology"
PERMISSION_READ_DASHBOARD = "read_dashboard"
PERMISSION_READ_REPORTS = "read_reports"

ALL_PERMISSIONS = frozenset(
    {
        PERMISSION_MANAGE_USERS,
        PERMISSION_MANAGE_ROLES,
        PERMISSION_CONFIG_GATEWAY,
        PERMISSION_CONFIG_MODELS,
        PERMISSION_CONFIG_STORAGE,
        PERMISSION_CONFIG_AUTH,
        PERMISSION_CONFIG_BACKUP,
        PERMISSION_ALL_AUDIT,
        PERMISSION_DELETE,
        PERMISSION_MANAGE_PROJECTS,
        PERMISSION_EDIT_PROJECT,
        PERMISSION_ASSIGN,
        PERMISSION_MANAGE_STRUCTURE,
        PERMISSION_STYLE_GUIDES,
        PERMISSION_MANAGE_GLOSSARY,
        PERMISSION_START_TRANSLATION,
        PERMISSION_REVIEW_SEGMENTS,
        PERMISSION_APPROVE_SEGMENTS,
        PERMISSION_COMPARE,
        PERMISSION_ANNOTATE_QM,
        PERMISSION_APPROVE_TERMINOLOGY,
        PERMISSION_READ_DASHBOARD,
        PERMISSION_READ_REPORTS,
    }
)


# --- Role -> permission matrix (PRD §2.1) -------------------------------
def _(*perms: str) -> set[str]:
    return set(perms)


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": set(ALL_PERMISSIONS),
    "project_manager": _(
        PERMISSION_MANAGE_PROJECTS,
        PERMISSION_EDIT_PROJECT,
        PERMISSION_ASSIGN,
        PERMISSION_MANAGE_STRUCTURE,
        PERMISSION_STYLE_GUIDES,
    ),
    "translator": _(
        PERMISSION_MANAGE_STRUCTURE,
        PERMISSION_MANAGE_GLOSSARY,
        PERMISSION_START_TRANSLATION,
        PERMISSION_REVIEW_SEGMENTS,
        PERMISSION_APPROVE_SEGMENTS,
    ),
    "revisor": _(
        PERMISSION_COMPARE,
        PERMISSION_ANNOTATE_QM,
        PERMISSION_APPROVE_TERMINOLOGY,
        PERMISSION_APPROVE_SEGMENTS,
    ),
    "qa_reader": _(PERMISSION_READ_DASHBOARD, PERMISSION_READ_REPORTS),
}


def has_role(role: str, permission: str) -> bool:
    """Return True when *role* holds *permission*.

    Unknown or empty roles hold nothing (fail closed). The ``admin`` role
    holds every permission by virtue of being assigned ``ALL_PERMISSIONS``.
    """
    if not role:
        return False
    perms = ROLE_PERMISSIONS.get(role)
    if perms is None:
        return False
    return permission in perms


def has_any_role(role: str, *required: str) -> bool:
    """Return True when *role* holds any of *required* permissions."""
    return any(has_role(role, r) for r in required)


# --- FastAPI authorization dependencies ---------------------------------
def _decode_token(authorization: str | None) -> dict[str, Any]:
    # AUTH_DISABLED=1 (2026-10-01, richiesta utente): il portale e' aperto,
    # ogni richiesta anonima e' trattata come admin. Il gate resta attivo
    # per chi manda un token: un token INVALIDO resta un 401 (un client
    # con una sessione scaduta non deve mai passare per sbaglio).
    if config.AUTH_DISABLED:
        if not authorization:
            return {"sub": "anonymous", "role": "admin", "type": "access"}
        token = authorization[len("bearer "):].strip() \
            if authorization.lower().startswith("bearer ") else ""
        if not token:
            return {"sub": "anonymous", "role": "admin", "type": "access"}
        try:
            payload = decode(token)
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
            ) from exc
        if payload.get("type") != "access":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="not an access token",
            )
        return payload
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )
    token = authorization[len("bearer "):].strip()
    try:
        payload = decode(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not an access token",
        )
    return payload


def get_current_user(authorization: str = Header(None)) -> dict[str, Any]:
    """Authentication dependency: any valid access token, any active role.

    This is the router-level gate applied to every domain router (see
    ``main.create_app``): the §2.1 five-role matrix is then enforced per
    route with :func:`require_permission` / :func:`require_admin`.
    """
    return _decode_token(authorization)


def _require_permissions(*required: str) -> Callable[..., dict[str, Any]]:
    def dependency(authorization: str = Header(None)) -> dict[str, Any]:
        payload = _decode_token(authorization)
        role = payload.get("role")
        if not has_any_role(role, *required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient permissions",
            )
        return payload

    return dependency


def require_permission(permission: str) -> Callable[..., dict[str, Any]]:
    """Dependency requiring a single *permission* (or admin)."""
    return _require_permissions(permission)


def require_admin(
    authorization: str = Header(None),
) -> dict[str, Any]:
    """Dependency requiring the ``admin`` role.

    This is the exact mechanism behind Acceptance Criterion 2: any endpoint
    using :func:`require_admin` returns 403 for every non-admin role.
    """
    payload = _decode_token(authorization)
    if payload.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin-only endpoint",
        )
    return payload


def require_any(*roles: str) -> Callable[..., dict[str, Any]]:
    """Dependency requiring any of *roles* to be present in the token."""
    return _require_permissions(*roles)
