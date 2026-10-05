"""Celery application for the Trans worker pool.

Jobs run exclusively against local resources: the local DB, MinIO, and the
LLM Gateway LLM endpoint. Nothing is sent to a cloud provider.
"""
from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://trans:trans@db:5432/trans")

app = Celery(
    "trans",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # Idempotent jobs with controlled retries (see PRD §4.2 / §14).
    task_default_retry_delay=10,
    task_max_retries=3,
    broker_connection_retry_on_startup=True,
)

# Discover tasks from the jobs module.
app.autodiscover_tasks(["worker"])
# L1 extraction tasks (PRD 5.2). Imported explicitly: the worker package has
# no Django-style module convention, so autodiscovery alone can miss it.
try:  # pragma: no cover - import side effect only
    from . import l1_tasks  # noqa: F401,E402
except ImportError:  # pragma: no cover - backend package not installed
    pass
# OCR tasks (PRD 5.2 step 4, dedicated 'ocr' queue, 14).
try:  # pragma: no cover - import side effect only
    from . import ocr_tasks  # noqa: F401,E402
except ImportError:  # pragma: no cover - backend package not installed
    pass


@app.on_after_configure.connect
def setup_periodic_tasks(sender: Celery, **_: object) -> None:
    # Placeholder for scheduled maintenance (e.g. DB snapshots).
    pass
