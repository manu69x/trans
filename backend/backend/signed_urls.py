"""Temporarily-signed download URLs (PRD §13.1).

Download links for stored documents must be *signed* and *temporary*: a
client receives a short-lived token bound to the exact resource, and the
serving endpoint refuses anything expired, tampered, or re-used after its
window. This module is deliberately dependency-free: the token is
``<expiry-epoch>.<hex HMAC-SHA256>`` over ``resource:expiry`` keyed with the
application secret (``JWT_SECRET``), so it cannot be forged without the key
and cannot outlive ``exp``.

* :func:`sign_url` -- issuer (used by the API route that hands out links).
* :func:`verify_url` -- verifier (used by the public download route).
* :func:`parse_expiry` -- read the embedded expiry for diagnostics.

The HMAC key defaults to ``config.JWT_SECRET`` so no second secret must be
distributed; ``TRANS_URL_SIGNING_KEY`` can override it in production
(kept outside the repo, PRD §13.1).
"""
from __future__ import annotations

import hmac
import hashlib
import os
import time

DEFAULT_TTL_SECONDS = 300


def _key() -> bytes:
    key = os.getenv("TRANS_URL_SIGNING_KEY", "") or os.getenv("JWT_SECRET", "")
    return key.encode("utf-8")


def _mac(resource: str, exp: int) -> str:
    msg = f"{resource}:{exp}".encode("utf-8")
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def sign_url(resource: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS,
             now: float | None = None) -> dict:
    """Return ``{"token", "exp", "url_path"}`` for *resource*.

    ``url_path`` is the querystring (``exp=...&token=...``) to append to the
    public download route. The token is bound to *resource* and expires at
    ``now + ttl_seconds`` (monotonic-independent wall-clock epoch).
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    exp = int((now if now is not None else time.time()) + ttl_seconds)
    token = _mac(resource, exp)
    return {
        "token": token,
        "exp": exp,
        "url_path": f"exp={exp}&token={token}",
    }


def verify_url(resource: str, token: str, exp: int | str,
               *, now: float | None = None) -> bool:
    """True when *token* is a valid signature for *resource* and unexpired.

    Constant-time comparison; expired or malformed inputs return False
    instead of raising so the route can answer a uniform 403.
    """
    try:
        exp_i = int(exp)
    except (TypeError, ValueError):
        return False
    now_f = time.time() if now is None else now
    if exp_i < now_f:
        return False
    if not token or not isinstance(token, str):
        return False
    expected = _mac(resource, exp_i)
    return hmac.compare_digest(expected, token)


def parse_expiry(token_url_path: str) -> int | None:
    """Extract the ``exp`` field of a signed querystring (diagnostics only)."""
    for part in token_url_path.split("&"):
        if part.startswith("exp="):
            try:
                return int(part[4:])
            except ValueError:
                return None
    return None
