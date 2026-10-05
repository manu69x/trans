"""Trans FastAPI application entrypoint.

Local-only MVP skeleton: health endpoint, DB (pgvector) connectivity check,
and a minimal project scaffold for the EN->IT literary translation platform.
All inference is routed exclusively through the local LLM Gateway.
"""
from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .db import Base
from .routes import router as api_router
from .auth_routes import router as auth_router  # noqa: E402
from .entity_routes import router as entity_router  # noqa: E402
from .structure_routes import router as structure_router  # noqa: E402
from .gateway_routes import router as gateway_router  # noqa: E402
from .glossary_routes import router as glossary_router  # noqa: E402
from .translation_routes import router as translation_router  # noqa: E402
from .editor_routes import router as editor_router  # noqa: E402
from .export_routes import router as export_router  # noqa: E402
from .progress_routes import router as progress_router  # noqa: E402
from .queue_routes import router as queue_router  # noqa: E402


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://trans:trans@db:5432/trans")


def create_app() -> FastAPI:
    engine = create_engine(_database_url())
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # §16: i job rimasti queued/running dopo un riavvio non hanno più un
        # worker: marcati failed con motivo esplicito (recupero al boot).
        try:
            from .scheduler import recover_stale_jobs

            recovered = recover_stale_jobs()
            if recovered:
                import logging

                logging.getLogger(__name__).warning(
                    "recover_stale_jobs: %d job orfani marcati failed", recovered
                )
        except Exception:  # pragma: no cover - startup must not block boot
            pass
        # §2.1: bootstrap admin from SEED_ADMIN_EMAIL/PASSWORD (idempotent;
        # no-op when the vars are unset). Startup must not block on it.
        try:
            from .seed import seed_admin_from_env

            seed_admin_from_env()
        except Exception:  # pragma: no cover - startup must not block boot
            pass
        # §8.1: populate the capability matrix from the real proxy on startup.
        try:
            from . import gateway as gateway_mod

            await gateway_mod.get_adapter().refresh()
        except Exception:  # pragma: no cover - startup must not block boot
            pass
        yield
        engine.dispose()

    app = FastAPI(title="Trans", version="0.1.0", lifespan=lifespan)
    # CORS (local-only): il browser può chiamare il backend direttamente per
    # le richieste LUNGHE (azioni massive LLM) che il proxy rewrite di Next
    # tronca a ~30s ("socket hang up"). Solo origin locali del LAN/host.
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        # local-only (§13): qualsiasi host della rete locale (localhost, LAN
        # privata) sulla porta del frontend. Regex necessaria perché chi
        # usa il frontend da LAN ha Origin http://<ip-lan>:3002.
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1"
            r"|192\.168\.[\d.]+|10\.[\d.]+"
            r"|172\.(1[6-9]|2\d|3[01])\.[\d.]+)(:\d+)?$"
        ),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
        # longApi() direct: senza expose il browser nasconde Content-Disposition
        # al JS e il download salva come "libro.pdf" (fix 2026-10-02).
        expose_headers=["Content-Disposition", "X-Export-Snapshot-Id"],
    )
    # §13/§14: register the 429 handler for the token-bucket limiter.
    from .routes import _rate_limit_exception_handler

    _rate_limit_exception_handler(app)
    # §2.1: every domain router requires a valid access token (any role);
    # the per-route §2.1 permission matrix is enforced with require_permission
    # inside each router. /auth/* stays open (login/refresh/logout) and
    # /health* is defined directly on the app for infra probes.
    from .rbac import get_current_user

    authenticated = [Depends(get_current_user)]
    app.include_router(api_router, prefix="/api/v1", dependencies=authenticated)
    app.include_router(auth_router, prefix="/api/v1", tags=["auth"])
    app.include_router(structure_router, prefix="/api/v1", tags=["structure"],
                       dependencies=authenticated)
    app.include_router(entity_router, prefix="/api/v1", tags=["entities"],
                       dependencies=authenticated)
    app.include_router(gateway_router, prefix="/api/v1", tags=["gateway"],
                       dependencies=authenticated)
    app.include_router(glossary_router, prefix="/api/v1", tags=["glossary"],
                       dependencies=authenticated)
    app.include_router(translation_router, prefix="/api/v1", tags=["translation"],
                       dependencies=authenticated)
    app.include_router(editor_router, prefix="/api/v1", tags=["editor"],
                       dependencies=authenticated)
    app.include_router(export_router, prefix="/api/v1", tags=["export"],
                       dependencies=authenticated)
    app.include_router(progress_router, prefix="/api/v1",
                       tags=["translation"], dependencies=authenticated)
    app.include_router(queue_router, prefix="/api/v1", tags=["queue"],
                       dependencies=authenticated)

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        # AUTH_DISABLED esposto al client: il frontend sa se deve richiedere
        # il login o mostrare direttamente l'app (2026-10-01, portale aperto).
        from .config import AUTH_DISABLED

        return {"status": "ok", "service": "trans-backend",
                "auth_disabled": AUTH_DISABLED}

    @app.get("/api/v1/health", tags=["meta"])
    async def api_health() -> dict:
        # Stesso payload sotto il prefisso /api/v1: il rewrite Next.js
        # mappa SOLO /api/v1/* verso il backend, quindi il probe auth del
        # frontend deve trovare il flag qui (fix 2026-10-01: prima era
        # solo su /health e il probe riceveva 404 -> redirect al login).
        from .config import AUTH_DISABLED

        return {"status": "ok", "service": "trans-backend",
                "auth_disabled": AUTH_DISABLED}

    @app.get("/health/db", tags=["meta"])
    async def health_db() -> dict:
        try:
            with SessionLocal() as s:
                row = s.execute(text("SELECT 1")).scalar_one()
                has_vector = (
                    s.execute(
                        text(
                            "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
                        )
                    ).scalar_one_or_none()
                    is not None
                )
            return {
                "status": "ok",
                "db": True,
                "pgvector": bool(has_vector),
                "check": row,
            }
        except Exception as exc:  # pragma: no cover
            raise HTTPException(status_code=503, detail=f"db unavailable: {exc}")

    return app


app = create_app()


def run() -> None:  # console-script entrypoint
    import subprocess

    sys.exit(
        subprocess.call(
            ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
        )
    )


if __name__ == "__main__":
    run()
