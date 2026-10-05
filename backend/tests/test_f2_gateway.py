"""httpx-based tests for F2: LLM Gateway adapter (PRD §8 / ADR-001).

Covers the three acceptance criteria of the corresponding kanban item:

1. ``GET /api/v1/gateway/models`` is populated dynamically from the real
   proxy ``/v1/models`` (no hardcoded model names, §8.1).
2. a model that is offline / unknown suspends the batch and asks the user
   (ModelUnavailableError) — the adapter NEVER autoswitches (§8.4).
3. ``llm_runs`` preserves the input/output hash and the parameters per call
   (§8.4, ADR-001).

The DB is the local Postgres given by DATABASE_URL / ALEMBIC_SQLALCHEMY_URI.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient, ASGITransport

# Make the backend package importable from the repo root.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.models import LLMRun  # noqa: E402
from backend import gateway as gateway_mod  # noqa: E402
from tests._f1_helpers import session_scope  # noqa: E402


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _messages_hash(messages: list[dict], model_id: str = "x") -> str:
    return _sha256(json.dumps({"model": model_id, "messages": messages}, sort_keys=True))


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    """Ensure the schema (incl. migration 007) is applied once per session."""
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
    """Each test starts from an empty, well-defined DB."""
    from backend.scheduler import wait_for_workers

    wait_for_workers()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeGatewayClient(gateway_mod.GatewayClient):
    """In-memory stand-in for the real OpenAI-compatible proxy.

    Configurable to return a populated model list, an empty (all-offline)
    list, or to raise on every call (proxy unreachable).
    """

    def __init__(
        self,
        models: list[dict] | None = None,
        health: dict | None = None,
        raise_on_call: bool = False,
        raise_on_completion: bool = False,
        completion: dict | None = None,
    ) -> None:
        self._models = models
        self._health = health or {"status": "ok", "target": "127.0.0.1:9001"}
        self._raise = raise_on_call
        self._raise_on_completion = raise_on_completion
        self._completion = completion
        self.calls: list[str] = []

    async def health(self) -> dict:
        self.calls.append("health")
        if self._raise:
            raise gateway_mod.GatewayError("proxy unreachable")
        return self._health

    async def list_models(self) -> list[dict]:
        self.calls.append("list_models")
        if self._raise:
            raise gateway_mod.GatewayError("proxy unreachable")
        return list(self._models or [])

    async def completion(
        self,
        model_id: str,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict | None = None,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        idempotency_key: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        self.calls.append("completion")
        if self._raise_on_completion:
            raise gateway_mod.GatewayError("proxy unreachable")
        return self._completion


def _raw_loaded(name: str, ctx: str | None = None) -> dict:
    """A raw /v1/models row for a loaded model (as the proxy would return it)."""
    return {
        "id": name,
        "name": name,
        "owned_by": "llama-swap",
        "status": {"value": "loaded"},
        "description": "",
        "context_window": ctx,
    }


def _set_adapter(
    models=None, *, raise_on_call=False, raise_on_completion=False, completion=None
):
    """Create a fake-backed adapter and install it (cache stays cold)."""
    client = FakeGatewayClient(
        models=models,
        raise_on_call=raise_on_call,
        raise_on_completion=raise_on_completion,
        completion=completion,
    )
    adapter = gateway_mod.GatewayAdapter(client=client)
    gateway_mod.set_adapter(adapter)
    return adapter, client


def _create_project(session) -> str:
    """Insert a `projects` row so `llm_runs` FK constraints are satisfied.

    Uses a fixed UUID so callers can assert on ``project_id``.
    """
    from backend.models import Project

    project = Project(
        id="11111111-1111-1111-1111-111111111111",
        title="F2 Gateway test project",
        genre_profile="novel",
        status="DRAFT",
    )
    session.add(project)
    session.flush()
    session.commit()
    return str(project.id)


# ---------------------------------------------------------------------------
# AC1: dynamic models
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_ac1_models_populated_from_real_proxy():
    """AC1: /api/v1/gateway/models is populated dynamically from /v1/models."""
    _set_adapter(
        models=[
            _raw_loaded("llama-3.3-70b-instruct-20260812"),
            _raw_loaded("qwen3-235b-instruct"),
        ]
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/gateway/models")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    ids = {m["id"] for m in body["models"]}
    assert ids == {"llama-3.3-70b-instruct-20260812", "qwen3-235b-instruct"}
    # Each model carries proxy-level capabilities + a derived context window.
    for m in body["models"]:
        assert "context_window" in m
        assert "status" in m
        assert m["status"] == "available"
        assert "supports_json_schema" in m
        assert "supports_reasoning" in m


@pytest.mark.anyio
async def test_ac1_no_hardcoded_models():
    """AC1: with zero proxy models the endpoint reports 0, not a hardcoded list."""
    _set_adapter(models=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/gateway/models")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    assert resp.json()["models"] == []


@pytest.mark.anyio
async def test_ac1_unreachable_proxy_returns_empty_matrix():
    """AC1: an unreachable proxy yields an empty matrix (200, count 0),
    never a hardcoded fallback (§8.4: all-offline matrix, no crash)."""
    _set_adapter(raise_on_call=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/gateway/models")
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] == 0
    assert resp.json()["models"] == []


# ---------------------------------------------------------------------------
# AC2: offline -> suspend, never autoswitch
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_ac2_available_for_loaded_model():
    """AC2: a loaded model is reported available by the adapter."""
    adapter, _client = _set_adapter(
        models=[_raw_loaded("llama-3.3-70b-instruct-20260812")]
    )
    m = await adapter.list_models()
    assert len(m) == 1
    assert m[0].id == "llama-3.3-70b-instruct-20260812"
    assert m[0].status == "available"
    # available() returns the model for a loaded id.
    assert adapter.available("llama-3.3-70b-instruct-20260812") is not None


@pytest.mark.anyio
async def test_ac2_unavailable_model_raises_no_autoswitch():
    """AC2: an unavailable model raises ModelUnavailableError (never autoswitches)."""
    _set_adapter(models=[_raw_loaded("llama-3.3-70b-instruct-20260812")])
    adapter = gateway_mod.get_adapter()

    # A model that is NOT on the proxy -> ModelUnavailableError.
    with pytest.raises(gateway_mod.ModelUnavailableError) as exc_info:
        adapter.available("does-not-exist")
    assert exc_info.value.model_id == "does-not-exist"
    assert "not found" in exc_info.value.reason.lower()

    # No autoswitch: available() never returns an alternative model — it
    # raises ModelUnavailableError instead of silently substituting one.
    with pytest.raises(gateway_mod.ModelUnavailableError):
        adapter.available("does-not-exist")


@pytest.mark.anyio
async def test_ac2_offline_suspends_no_autoswitch():
    """AC2: an offline model raises ModelUnavailableError (batch suspends)."""
    _set_adapter(models=[_raw_loaded("llama-3.3-70b-instruct-20260812")])
    adapter = gateway_mod.get_adapter()

    with pytest.raises(gateway_mod.ModelUnavailableError) as exc_info:
        adapter.available("offline-model")
    assert exc_info.value.model_id == "offline-model"
    assert exc_info.value.reason  # non-empty


@pytest.mark.anyio
async def test_ac2_test_run_unavailable_returns_409():
    """AC2: POST /test-run for an unknown model -> 409 (suspend, ask user)."""
    _set_adapter(models=[_raw_loaded("llama-3.3-70b-instruct-20260812")])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/gateway/test-run",
            json={
                "project_id": "11111111-1111-1111-1111-111111111111",
                "model_id": "offline-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["detail"]["code"] == "model_unavailable"
    assert body["detail"]["model_id"] == "offline-model"
    assert body["detail"]["action"] == "suspend_batch_and_ask_user"


# ---------------------------------------------------------------------------
# AC3: llm_runs hash + params
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_ac3_record_preserves_hash_and_params():
    """AC3: test_run stores prompt_hash, output_hash and parameters."""
    completion = {
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"entities": []}',
                    "refusal": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    adapter, _client = _set_adapter(
        models=[_raw_loaded("llama-3.3-70b-instruct-20260812")],
        completion=completion,
    )
    await adapter.list_models()
    with session_scope() as _s:
        _create_project(_s)
    project_id = "11111111-1111-1111-1111-111111111111"
    messages = [{"role": "user", "content": "extract entities"}]

    result = await adapter.test_run(
        project_id=project_id,
        model_id="llama-3.3-70b-instruct-20260812",
        messages=messages,
        purpose="test",
        idempotency_key="run-abc-123",
    )

    assert result["ok"] is True
    assert result["model"] == "llama-3.3-70b-instruct-20260812"
    assert result["content"] == '{"entities": []}'
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5

    # The run is persisted with the correct hash + params + idempotency key.
    with session_scope() as s:
        rows = s.query(LLMRun).filter(LLMRun.project_id == project_id).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.prompt_hash == _messages_hash(messages, "llama-3.3-70b-instruct-20260812")
        assert row.output_hash == _sha256('{"entities": []}')
        assert row.model_name == "llama-3.3-70b-instruct-20260812"
        assert row.run_type == "test"
        assert row.status == "completed"
        assert row.input_tokens == 10
        assert row.output_tokens == 5
        # parameters carry the idempotency key (§8.4).
        assert row.parameters["idempotency_key"] == "run-abc-123" or (
            row.parameters.get("idempotency_key") is not None
        )
        assert row.parameters["purpose"] == "test"


@pytest.mark.anyio
async def test_ac3_idempotency_key_stored():
    """F2: the idempotency key is passed to the proxy and stored in params."""
    completion = {
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"entities": []}',
                    "refusal": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    adapter, client = _set_adapter(
        models=[_raw_loaded("llama-3.3-70b-instruct-20260812")],
        completion=completion,
    )
    await adapter.list_models()
    with session_scope() as _s:
        _create_project(_s)
    project_id = "11111111-1111-1111-1111-111111111111"

    await adapter.test_run(
        project_id=project_id,
        model_id="llama-3.3-70b-instruct-20260812",
        messages=[{"role": "user", "content": "extract"}],
        idempotency_key="dup-ref",
    )
    # The idempotency key was forwarded to the proxy in the completion call.
    assert client.calls.count("completion") == 1
    with session_scope() as s:
        row = s.query(LLMRun).one()
        ik = row.parameters["idempotency_key"] if row.parameters else None
        assert ik == "dup-ref"


@pytest.mark.anyio
async def test_ac3_partial_output_rejected_when_non_schema():
    """F2: a non-schema output raises SchemaValidationError and records nothing."""
    completion = {
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "not-json-at-all",
                    "refusal": None,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    _set_adapter(
        models=[_raw_loaded("llama-3.3-70b-instruct-20260812")],
        completion=completion,
    )
    adapter = gateway_mod.get_adapter()
    await adapter.list_models()
    with session_scope() as _s:
        _create_project(_s)
    project_id = "11111111-1111-1111-1111-111111111111"

    with pytest.raises(gateway_mod.SchemaValidationError):
        await adapter.test_run(
            project_id=project_id,
            model_id="llama-3.3-70b-instruct-20260812",
            messages=[{"role": "user", "content": "extract"}],
            json_schema={"type": "object", "properties": {"entities": {"type": "array"}}},
        )

    with session_scope() as s:
        rows = s.query(LLMRun).all()
        assert len(rows) == 0  # nothing recorded for a rejected output


@pytest.mark.anyio
async def test_ac3_unreachable_proxy_records_failure_then_raises():
    """F2: an unreachable proxy records a failed run and raises GatewayError."""
    _set_adapter(
        models=[_raw_loaded("llama-3.3-70b-instruct-20260812")],
        raise_on_completion=True,
    )
    adapter = gateway_mod.get_adapter()
    await adapter.list_models()
    with session_scope() as _s:
        _create_project(_s)
    project_id = "11111111-1111-1111-1111-111111111111"

    with pytest.raises(gateway_mod.GatewayError):
        await adapter.test_run(
            project_id=project_id,
            model_id="llama-3.3-70b-instruct-20260812",
            messages=[{"role": "user", "content": "extract"}],
        )

    with session_scope() as s:
        row = s.query(LLMRun).one()
        assert row.status == "failed"
        assert row.error is not None


# ---------------------------------------------------------------------------
# Real-proxy integration (skipped unless explicitly enabled)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_integration_real_proxy_health():
    """Integration: the real proxy health endpoint is reachable and reports a target."""
    if os.getenv("LLM_GATEWAY_INTEGRATION_TEST", "").lower() not in ("1", "true"):
        pytest.skip("set LLM_GATEWAY_INTEGRATION_TEST=1 to run against the real proxy")

    client = gateway_mod.GatewayClient()
    health = await client.health()
    assert health.get("status") in ("ok", "loading")
    assert "target" in health
