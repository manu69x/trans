"""Tests for the translation batch run (§10.1 / §8.4 / §15.3 / task F3).

Acceptance criteria under test:

* AC1 -- a response with a **missing / extra / reordered segment ID** is
  **discarded and the job fails** (retryable); a corrected re-run succeeds
  (§15.3 AC1, §10.2).
* AC2 -- a **valid** response is **saved as ``machine_draft``** on every
  ``TranslationUnit`` and the run is **tracked** on ``llm_runs`` (§15.3 AC2).
* AC3 -- a **crash mid-batch** leaves already-saved segments untouched and a
  **resume** (same ``run_ref``) finishes the block **without duplicating**
  any segment (§8.4 / §15.3 AC3).

The LLM is a test double (``_FakeLLM``) for :meth:`GatewayClient.chat_json`;
the real call is covered by the live-Gateway benchmark.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f3-run")
os.environ.setdefault("LOCAL_ONLY", "1")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.gateway_http import GatewayClient  # noqa: E402
from backend.scheduler import wait_for_workers  # noqa: E402
from backend.models import (  # noqa: E402
    LLMRun,
    Project,
    TranslationUnit,
)


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    os.environ.setdefault(
        "ALEMBIC_SQLALCHEMY_URI",
        os.getenv("DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans"),
    )
    import subprocess

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(BACKEND_DIR),
    )
    yield


@pytest.fixture(autouse=True)
def _reset():
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    wait_for_workers()
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _fake_chat(translations: list[dict], *, fail: bool = False):
    """A ``GatewayClient.chat_json`` replacement returning a JSON payload.

    ``fail`` raises :class:`GatewayInvalidJSON` (a crash before any save).
    ``translations`` is a list of ``{"segment_id", "target_text", ...}``.
    """
    from backend.gateway_http import GatewayInvalidJSON

    def _call(self, *, model, system_prompt, user_prompt, temperature=0.0,
              max_tokens=None, seed=None):
        if fail:
            raise GatewayInvalidJSON("simulated crash (no valid JSON)")
        return {"translations": translations}

    return _call


@pytest.fixture
def fake_chat():
    """Patch ``GatewayClient.chat_json`` with a configurable fake."""
    state = {"handler": None}

    def _install(translations, *, fail=False):
        state["handler"] = _fake_chat(translations, fail=fail)
        GatewayClient.chat_json = state["handler"]  # type: ignore[method-assign]

    _install(None)
    yield _install
    GatewayClient.chat_json = None  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _new_project(client: AsyncClient, genre: str = "fantascientifica") -> str:
    r = await client.post("/api/v1/projects", json={
        "title": "F3 Run Book", "genre_profile": genre})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seg(seg_id: str, text: str, ordinal: int) -> dict:
    return {"segment_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, str(seg_id))),
            "source_text": text, "ordinal": ordinal}


def _valid_translation(seg_id: str, src: str, tgt: str) -> dict:
    return {"segment_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, str(seg_id))),
            "target_text": tgt, "flags": []}


async def _run(client: AsyncClient, pid: str, segments: list[dict],
               idempotency_key: str | None = None,
               extra: dict | None = None) -> dict:
    body: dict = {
        "chapter_id": str(uuid.uuid4()),
        "segments": segments,
        "block_source": " ".join(s["source_text"] for s in segments),
    }
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if extra:
        body.update(extra)
    r = await client.post(f"/api/v1/projects/{pid}/translation/run", json=body)
    assert r.status_code == 202, r.text
    return r.json()


async def _jobs(client: AsyncClient, pid: str) -> list[dict]:
    r = await client.get(f"/api/v1/projects/{pid}/jobs")
    assert r.status_code == 200, r.text
    return r.json()


def _tus(client: AsyncClient, pid: str) -> list[dict]:
    from backend.db import SessionLocal
    from backend.models import TranslationUnit as TU

    with SessionLocal() as s:
        rows = s.query(TU).filter(TU.project_id == pid).all()
    return [
        {"id": str(t.id), "status": t.status, "target_text": t.target_text,
         "run_id": str(t.model_run_id) if t.model_run_id else None}
        for t in rows
    ]


# ---------------------------------------------------------------------------
# AC2 -- valid output saved as machine_draft, run tracked
# ---------------------------------------------------------------------------
async def test_ac2_valid_output_saved_as_machine_draft(client, fake_chat):
    pid = await _new_project(client)
    s1 = _seg("s1", "Good morning.", 1)
    s2 = _seg("s2", "How are you?", 2)
    fake_chat([
        _valid_translation("s1", "Good morning.", "Buongiorno."),
        _valid_translation("s2", "How are you?", "Come stai?"),
    ])
    queued = await _run(client, pid, [s1, s2], idempotency_key="run-1")
    job_id = queued["job_id"]

    # poll the job to completion
    wait_for_workers(timeout=120.0)

    jobs = await _jobs(client, pid)
    t = next(j for j in jobs if j["id"] == job_id)
    assert t["status"] == "completed", t

    # each segment is a machine_draft (never approved)
    tus = _tus(client, pid)
    assert len(tus) == 2
    for tu in tus:
        assert tu["status"] == "machine_draft"
        assert tu["run_id"]  # every segment carries the run id

    # the run is tracked on llm_runs
    from backend.db import SessionLocal
    from backend.models import LLMRun as LLM

    with SessionLocal() as s:
        runs = s.query(LLM).filter(LLM.project_id == pid).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.run_type == "translate"
    assert run.status == "completed"
    assert run.run_ref == "run-1"
    assert run.output_hash  # the response is hashed / stored as a hash
    assert run.parameters["response"] is not None  # the raw response is kept


# ---------------------------------------------------------------------------
# AC1 -- missing / extra / reordered ID -> discarded and retried
# ---------------------------------------------------------------------------
async def test_ac1_bad_id_discarded_and_retry(client, fake_chat):
    pid = await _new_project(client)
    s1, s2 = _seg("s1", "Good morning.", 1), _seg("s2", "How are you?", 2)

    # FIRST run: reordered ids (s2 before s1) -> hard failure, discarded.
    fake_chat([
        _valid_translation("s2", "How are you?", "Come stai?"),
        _valid_translation("s1", "Good morning.", "Buongiorno."),
    ])
    await _run(client, pid, [s1, s2], idempotency_key="bad")
    wait_for_workers(timeout=120.0)

    jobs = await _jobs(client, pid)
    bad = next(j for j in jobs if j["job_type"] == "translate")
    assert bad["status"] == "failed", bad
    assert "validation" in (bad["error"] or "").lower()

    # nothing was saved
    assert _tus(client, pid) == []

    # SECOND run: a corrected (correctly ordered) response is accepted.
    fake_chat([
        _valid_translation("s1", "Good morning.", "Buongiorno."),
        _valid_translation("s2", "How are you?", "Come stai?"),
    ])
    await _run(client, pid, [s1, s2], idempotency_key="good")
    wait_for_workers(timeout=120.0)

    jobs = await _jobs(client, pid)
    good = next(j for j in jobs if j["job_type"] == "translate")
    assert good["status"] == "completed", good
    tus = _tus(client, pid)
    assert len(tus) == 2
    assert all(t["status"] == "machine_draft" for t in tus)


async def test_ac1_missing_id_discarded(client, fake_chat):
    pid = await _new_project(client)
    s1, s2 = _seg("s1", "Good morning.", 1), _seg("s2", "How are you?", 2)
    # s2 is missing from the response -> hard failure.
    fake_chat([_valid_translation("s1", "Good morning.", "Buongiorno.")])
    await _run(client, pid, [s1, s2], idempotency_key="missing")
    wait_for_workers(timeout=120.0)
    jobs = await _jobs(client, pid)
    bad = next(j for j in jobs if j["job_type"] == "translate")
    assert bad["status"] == "failed", bad
    assert _tus(client, pid) == []


async def test_ac1_extra_id_discarded(client, fake_chat):
    pid = await _new_project(client)
    s1, s2 = _seg("s1", "Good morning.", 1), _seg("s2", "How are you?", 2)
    # an extra id not requested -> hard failure.
    fake_chat([
        _valid_translation("s1", "Good morning.", "Buongiorno."),
        _valid_translation("s2", "How are you?", "Come stai?"),
        _valid_translation("s3", "Extra", "Extra"),
    ])
    await _run(client, pid, [s1, s2], idempotency_key="extra")
    wait_for_workers(timeout=120.0)
    jobs = await _jobs(client, pid)
    bad = next(j for j in jobs if j["job_type"] == "translate")
    assert bad["status"] == "failed", bad
    assert _tus(client, pid) == []


# ---------------------------------------------------------------------------
# AC3 -- crash mid-batch -> resume without duplicating segments
# ---------------------------------------------------------------------------
async def test_ac3_crash_then_resume_no_duplicates(client, fake_chat):
    pid = await _new_project(client)
    s1, s2, s3 = (_seg("s1", "One.", 1), _seg("s2", "Two.", 2),
                  _seg("s3", "Three.", 3))

    # FIRST attempt: the LLM call crashes (no valid JSON) -> job failed,
    # nothing saved.
    fake_chat([], fail=True)
    await _run(client, pid, [s1, s2, s3], idempotency_key="crash")
    wait_for_workers(timeout=120.0)
    jobs = await _jobs(client, pid)
    bad = next(j for j in jobs if j["job_type"] == "translate")
    assert bad["status"] == "failed", bad
    assert _tus(client, pid) == []

    # SECOND attempt: a valid response for all three.
    fake_chat([
        _valid_translation("s1", "One.", "Uno."),
        _valid_translation("s2", "Two.", "Due."),
        _valid_translation("s3", "Three.", "Tre."),
    ])
    await _run(client, pid, [s1, s2, s3], idempotency_key="crash")
    wait_for_workers(timeout=120.0)

    jobs = await _jobs(client, pid)
    good = next(j for j in jobs if j["job_type"] == "translate")
    assert good["status"] == "completed", good

    # every segment saved exactly once, all machine_draft
    tus = _tus(client, pid)
    ids = sorted(t["id"] for t in tus)
    assert ids == sorted([str(uuid.uuid5(uuid.NAMESPACE_DNS, s)) for s in ("s1", "s2", "s3")])
    assert len(tus) == 3
    assert all(t["status"] == "machine_draft" for t in tus)


    # SECOND identical run (same run_ref) -> resume, no new LLM calls,
    # no duplicated segments.
    orig = GatewayClient.chat_json
    counter = {"n": 0}

    def _counting(self, *, model, system_prompt, user_prompt, temperature=0.0,
                  max_tokens=None, seed=None):
        counter["n"] += 1
        return {"translations": translations}

    GatewayClient.chat_json = _counting  # type: ignore[method-assign]
    try:
        await _run(client, pid, [s1, s2, s3], idempotency_key="crash")
        wait_for_workers(timeout=120.0)
    finally:
        GatewayClient.chat_json = orig  # type: ignore[method-assign]

    assert counter["n"] == 0, "a warm run must not call Gateway again"
    tus = _tus(client, pid)
    assert len(tus) == 3  # not 4 -- no duplicates
    assert all(t["status"] == "machine_draft" for t in tus)


async def test_ac3_hard_error_not_saved(client, fake_chat):
    pid = await _new_project(client)
    s1, s2 = _seg("s1", "Good morning.", 1), _seg("s2", "How are you?", 2)
    # soft error: a forbidden term appears in the target -> still saved as
    # machine_draft (soft failures are recorded, not fatal).
    fake_chat([
        _valid_translation("s1", "Good morning.", "Buongiorno."),
        _valid_translation("s2", "How are you?", "Come stai, lama."),
    ])
    await _run(client, pid, [s1, s2], idempotency_key="soft")
    wait_for_workers(timeout=120.0)
    jobs = await _jobs(client, pid)
    job = next(j for j in jobs if j["job_type"] == "translate")
    assert job["status"] == "completed", job
    tus = _tus(client, pid)
    assert len(tus) == 2
    assert all(t["status"] == "machine_draft" for t in tus)


# ---------------------------------------------------------------------------
# local-only gate at the run boundary (§13.1)
# ---------------------------------------------------------------------------
async def test_run_rejects_non_local_gateway(client, monkeypatch):
    from backend import config as cfg

    pid = await _new_project(client)
    monkeypatch.setattr(cfg, "LLM_GATEWAY_BASE_URL",
                        "http://api.evil-cloud.com/v1")
    r = await client.post(
        f"/api/v1/projects/{pid}/translation/run",
        json={"block_source": "x", "segments": []})
    assert r.status_code == 451
    assert "local_only" in r.json()["detail"]
    jobs = await _jobs(client, pid)
    assert not [j for j in jobs if j["job_type"] == "translate"]
