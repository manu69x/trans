"""Application configuration (PRD §13.1 local_only policy, §2.1 roles).

All secrets are read from environment variables so that no production
secret is committed. The ``local_only`` policy implements §13.1: the
platform must never be configured to send manuscript data, glossary or
embeddings to a non-local endpoint. Any outbound LLM/HTTP call must pass
through :func:`assert_local_url` before it is issued.
"""

from __future__ import annotations

import os
from typing import Literal

# --- Core ---------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trans:trans@127.0.0.1/trans")

# --- Secrets ------------------------------------------------------------
# In production these MUST be provided via the environment. They default to
# clearly-non-secret local-only values so the seed script can run locally.
JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-change-me")
REFRESH_TOKEN_TTL_SECONDS = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(7 * 24 * 3600)))
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", str(15 * 60)))

# --- Seed admin (PRD: "Seed di un utente admin da env var") -------------
SEED_ADMIN_EMAIL = os.getenv("SEED_ADMIN_EMAIL", "")
SEED_ADMIN_PASSWORD = os.getenv("SEED_ADMIN_PASSWORD", "")

#: When ``1``/``true`` the app does not require login: every request without
#: an Authorization header is treated as admin. Intended for air-gapped
#: single-user machines only. Default OFF: authentication stays enabled
#: unless explicitly overridden via docker-compose (``AUTH_DISABLED``).
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0").lower() in ("1", "true", "yes")

# --- local_only policy (§13.1) -----------------------------------------
LOCAL_ONLY = os.getenv("LOCAL_ONLY", "1") == "1"

# Hosts considered "local". Anything whose hostname is not in this set is
# blocked by :func:`assert_local_url` when ``LOCAL_ONLY`` is enabled.
_LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "trans-db",  # compose service name (see docker-compose)
    "db",
    # Docker Desktop built-in names: they resolve inside the host VM /
    # compose network and can never route outside the machine (§13.1).
    "host.docker.internal",
    "minio",
    "backend",
    "frontend",
    "redis",
}


def _is_local_host(hostname: str) -> bool:
    host = hostname.lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


def is_local_url(url: str) -> bool:
    """Return True when *url* resolves to a local endpoint.

    Used by the local_only policy (§13.1) to reject outbound calls to
    non-local hosts before any manuscript data can leave the network.
    """
    if not url:
        return False
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    host = host.split(":  ")[0]  # strip any :port
    return _is_local_host(host)


def assert_local_url(url: str) -> str:
    """Raise if *url* is not local and ``LOCAL_ONLY`` is enabled.

    This is the enforcement point for §13.1: it blocks or flags any
    outbound call (LLM Gateway, object storage, telemetry) whose host is
    not in the local set.
    """
    if not LOCAL_ONLY:
        return url
    if not is_local_url(url):
        raise ValueError(f"local_only policy (§13.1) blocks non-local endpoint: {url}")
    return url


# --- LLM Gateway (PRD §8, ADR-001) -------------------------------------
# The only LLM inference source (§8.1): any OpenAI-compatible local server
# (llama.cpp / llama-swap / vLLM / LM Studio, ...). The default targets the
# host loopback, which stays inside the local-only policy (§13.1).
LLM_GATEWAY_BASE_URL = os.getenv("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:8080/v1")
# Segment quality-estimation service (see deploy/qe-service/README.md).
# By default it is derived from the gateway base URL (the gateway's
# ``/upstream/qe`` upstream), so one gateway can serve both the translation
# models and the verification model. Override with an explicit URL to point
# it at a standalone endpoint.
QE_BASE_URL = os.getenv("QE_BASE_URL") or (
    LLM_GATEWAY_BASE_URL.rstrip("/").removesuffix("/v1") + "/upstream/qe"
)
LLM_GATEWAY_API_KEY = os.getenv("LLM_GATEWAY_API_KEY", "sk-local-dev-change-me")
# HTTP timeout (seconds) for gateway calls (§8.1). A cold model start can
# take tens of seconds, so this is deliberately generous.
LLM_GATEWAY_TIMEOUT = int(os.getenv("LLM_GATEWAY_TIMEOUT", "60") or 60)
# Budget cap for one structured call (ADR-001 §3.3: max_tokens accepted;
# for models that ignore it, the adapter sets an upper bound). 16384 keeps
# a 16k-in / 16k-out block budget.
LLM_GATEWAY_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_GATEWAY_MAX_OUTPUT_TOKENS", "16384"))

# --- Default models for NEW projects (§8.1/§8.2: env-driven, no hardcoding
# in the create route — the operator pins the defaults per deployment).
DEFAULT_TRANSLATION_MODEL = os.getenv(
    "TRANS_DEFAULT_TRANSLATION_MODEL", "translategemma-27b-it")
# Fallback translation model: used automatically when the primary model
# returns the source unchanged (untranslated draft). Empty string disables
# the fallback; pick a model id that exists on YOUR gateway.
FALLBACK_TRANSLATION_MODEL = os.getenv("TRANS_FALLBACK_TRANSLATION_MODEL", "")
DEFAULT_TEXT_MODEL = os.getenv("TRANS_DEFAULT_TEXT_MODEL", "")
DEFAULT_MODEL_SETTINGS = {
    "temperature": float(os.getenv("TRANS_DEFAULT_TEMPERATURE", "0.2")),
    "top_p": float(os.getenv("TRANS_DEFAULT_TOP_P", "0.9")),
    "top_k": int(os.getenv("TRANS_DEFAULT_TOP_K", "40")),
    "reasoning_effort": os.getenv("TRANS_DEFAULT_REASONING", "none"),
    "max_output_tokens": int(os.getenv(
        "TRANS_DEFAULT_MAX_OUTPUT_TOKENS", str(LLM_GATEWAY_MAX_OUTPUT_TOKENS))),
}
# Rate limiting for LLM calls (§8.4): RPS cap + retry with backoff on 429/5xx.
LLM_MAX_RPS = float(os.getenv("LLM_MAX_RPS", "1.0"))
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# --- Roles (PRD §2.1) ---------------------------------------------------
# The five canonical roles. Each maps to the permission set granted in the
# database seed (see :mod:`backend.seed`).
ROLES = Literal[
    "admin",
    "project_manager",
    "translator",
    "revisor",
    "qa_reader",
]
