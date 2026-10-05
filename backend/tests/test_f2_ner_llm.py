"""Tests for the LLM-structured NER worker.

Acceptance criteria under test:

* AC1 — the LLM output is validated against the JSON Schema (§9.5) and the
  schema-compliance rate over all calls is ≥ 95% (guaranteed by design:
  schema-constrained calls + per-candidate validation; a mocked malformed
  model proves the failure is *counted*, not silently accepted).
* AC2 — provenance: every LLM proposal lands as evidence with
  ``extractor='llm:<model>'`` and a confidence; BookNLP attributions are
  never overwritten (field-level merge, §6.2.3; a PERSON stays PERSON).
* AC3 — local-only (§13.1): a non-local Gateway base URL is rejected by
  the API endpoint before any job is queued, and the adapter refuses to
  call it; the pipeline is exercised against the local proxy.

Additional criteria from the task body:

* selection scope: only §6.2.3 domain categories and low-confidence
  (high-ambiguity) candidates go to the LLM — a confident PERSON never does;
* idempotency per block (§13.2): the second identical run performs ZERO new
  LLM calls (cache hits on ``llm_runs`` keyed by block hash);
* rate limiting (§8.4): the pacesetter spaces calls at ≥ 1/RPS.

The LLM is a test double (``_FakeLLM``) for the deterministic criteria; the
real Gateway call is covered by the ``live_gateway`` benchmark script
(``docs/benchmarks/bench_ner_llm.py``) and the optional ``live_gateway``
test, both skipped when the proxy is not reachable.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-storage-nerllm")
os.environ.setdefault("LOCAL_ONLY", "1")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans")

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend import gateway_http  # noqa: E402
from backend.gateway_http import (  # noqa: E402
    GatewayClient,
    GatewayInvalidJSON,
    RateLimiter,
    reset_rate_limiter,
)
from backend.parsing import ner_llm  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    os.environ.setdefault(
        "ALEMBIC_SQLALCHEMY_URI", os.getenv("DATABASE_URL"))
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

    reset_rate_limiter()
    wait_for_workers()
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    wait_for_workers()
    engine.dispose()
    reset_rate_limiter()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _fantasy_pdf() -> bytes:
    """The fantasy chapter of the corpus, as an in-memory native PDF.

    Dense with §6.2.3 domain categories: the curse (the Withering), the
    species (the Wyrm, wyrmlings), the artefact (the Black Key), the
    fictional institution (the Order of the Pale Hand) + persons and
    locations, so the candidate selection has all categories.
    """
    import pymupdf

    txt_path = (REPO_ROOT / "docs" / "benchmarks" / "corpus" / "txt"
                / "native_05_fantasy_chapter.txt")
    body = txt_path.read_text(encoding="utf-8")

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((56, 40), "THE WITHERING", fontsize=9)
    y = 72.0
    for para in body.strip().split("\n\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if y > 720:
                break
            if len(trial) * 6.2 <= 500:
                line = trial
            else:
                page.insert_text((56, y), line, fontsize=11)
                y += 14.0
                line = word
        if line and y <= 720:
            page.insert_text((56, y), line, fontsize=11)
            y += 20.0
    data = doc.tobytes()
    doc.close()
    return data


async def _import_and_extract(client: AsyncClient) -> str:
    """Import the fantasy chapter, detect structure, run BookNLP+NER."""
    from backend.scheduler import wait_for_workers

    r = await client.post("/api/v1/projects", json={
        "title": "NER LLM Book", "genre_profile": "fantasy"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("fantasy.pdf", _fantasy_pdf(),
                        "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    document_id = r.json()["document_id"]
    wait_for_workers()
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.post(f"/api/v1/projects/{pid}/structure/detect")
    assert r.status_code == 202, r.text
    wait_for_workers()
    r = await client.post(f"/api/v1/projects/{pid}/entities/extract")
    assert r.status_code == 202, r.text
    wait_for_workers(timeout=480.0)
    return pid


async def _poll_job(client: AsyncClient, pid: str, job_id: str,
                    timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/v1/projects/{pid}/jobs")
        assert r.status_code == 200, r.text
        job = next(j for j in r.json() if j["id"] == job_id)
        if job["status"] in ("completed", "failed"):
            return job
        if time.monotonic() > deadline:
            pytest.fail(f"job did not finish in {timeout}s: {job}")
        await asyncio.sleep(0.2)


class _FakeLLM:
    """Test double for :meth:`GatewayClient.chat_json`.

    Returns schema-valid classifications for the candidates it is given;
    ``bad_blocks`` makes specific block indexes return a malformed payload
    (to prove schema failures are counted, AC1); every call is recorded.
    """

    def __init__(self, categories: dict[str, str] | None = None,
                 bad_blocks: set[int] | None = None) -> None:
        self.calls: list[dict] = []
        self.categories = categories or {}
        self.bad_blocks = bad_blocks or set()

    def __call__(self, *, model, system_prompt, user_prompt,
                 temperature=0.0, max_tokens=None, seed=None):
        import json

        self.calls.append({"model": model, "temperature": temperature,
                           "user_len": len(user_prompt)})
        index = len(self.calls) - 1
        if index in self.bad_blocks:
            return {"unexpected": "shape"}  # valid JSON, schema-invalid
        # rebuild the candidate list from the prompt payload
        payload = json.loads(user_prompt.split("Candidates:\n", 1)[1])
        out = []
        for item in payload:
            out.append({
                "id": item["id"],
                "category": self.categories.get(item["name"],
                                                "CREATURE_SPECIES"),
                "confidence": 0.9,
                "evidence_quote": (item["examples"][0]
                                   if item["examples"] else item["name"]),
            })
        return {"classifications": out}


@pytest.fixture
def fake_llm():
    """Patch GatewayClient.chat_json and freeze model discovery."""
    fake = _FakeLLM()
    orig_chat = GatewayClient.chat_json
    orig_models = GatewayClient.list_models
    GatewayClient.chat_json = fake  # type: ignore[method-assign]
    GatewayClient.list_models = lambda self: [  # type: ignore[method-assign]
        {"id": "test-analysis-model", "display_name": "test",
         "provider": "llama-swap", "status": "loaded",
         "supports_json_schema": True, "supports_streaming": True,
         "context_window": 32768}]
    yield fake
    GatewayClient.chat_json = orig_chat  # type: ignore[method-assign]
    GatewayClient.list_models = orig_models  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# pure-layer selection: only §6.2.3 candidates reach the LLM
# --------------------------------------------------------------------------
def test_selection_scope_domain_and_ambiguity_only():
    text = ("The Withering crept closer. Kestra held the Black Key. "
            "Dain spoke of the Wyrm of the marsh. The Order of the Pale "
            "Hand had warned them long ago; the Pale King watched from "
            "above.")
    entities = [
        # domain categories: always selected
        {"canonical_source": "the Wyrm", "entity_type": "CONCEPT_TERM",
         "confidence": 0.9, "mention_count": 3},
        {"canonical_source": "the Order of the Pale Hand",
         "entity_type": "ORG_FACTION", "confidence": 0.95,
         "mention_count": 5},
        # high-ambiguity (low confidence): selected
        {"canonical_source": "Dain", "entity_type": "PERSON",
         "confidence": 0.42, "mention_count": 2},
        # confident PERSON: NEVER selected (§6.2.3 scope)
        {"canonical_source": "Kestra", "entity_type": "PERSON",
         "confidence": 0.93, "mention_count": 9},
        # article-initial PERSON: ambiguous *by surface* — curse-like
        # surfaces ("the Withering") always reach the LLM for review
        # evidence, regardless of confidence (merge never re-types them)
        {"canonical_source": "the Withering", "entity_type": "PERSON",
         "confidence": 0.65, "mention_count": 4},
        # same rule holds at high confidence (surface is the signal)
        {"canonical_source": "the Pale King", "entity_type": "PERSON",
         "confidence": 0.85, "mention_count": 6},
    ]
    got = ner_llm.select_candidates(entities, text)
    names = {c.name for c in got}
    assert "the Wyrm" in names          # CONCEPT_TERM → domain scope
    assert "the Order of the Pale Hand" in names
    assert "Dain" in names              # low confidence → ambiguous
    assert "Kestra" not in names        # confident person → excluded
    assert "the Withering" in names     # surface-ambiguous → selected
    assert "the Pale King" in names     # surface rule, no confidence cap


def test_selection_needs_a_quotable_context():
    entities = [{"canonical_source": "Zibaldone", "entity_type": "CONCEPT_TERM",
                 "confidence": 0.9, "mention_count": 1}]
    assert ner_llm.select_candidates(entities, "nothing here") == []


def test_block_and_schema_shape():
    text = "The Wyrm slept beneath the manor."
    entities = [{"canonical_source": "the Wyrm",
                 "entity_type": "CONCEPT_TERM", "confidence": 0.5,
                 "mention_count": 1}]
    (cand,) = ner_llm.select_candidates(entities, text)
    block = ner_llm.build_block([cand], chapter_title="Chapter One")
    assert len(block["block_hash"]) == 16
    assert len(block["prompt_sha256"]) == 64
    assert cand.id in block["candidate_ids"]
    # the prompt never embeds the raw system message
    assert "JSON" in block["system"]


def test_chunking_respects_block_size():
    cands = [
        ner_llm.LLMCandidate(id=f"X:{i}", name=f"n{i}",
                             current_type="CONCEPT_TERM", confidence=0.4,
                             mention_count=1, quotes=[f"quote {i}"])
        for i in range(30)
    ]
    blocks = ner_llm.chunk_blocks(cands)
    assert len(blocks) == 3
    assert all(len(b["candidate_ids"]) <= 12 for b in blocks)


def test_parse_response_rejects_bad_and_keeps_good():
    cands = {
        "T:1": ner_llm.LLMCandidate(id="T:1", name="the Wyrm",
                                    current_type="CONCEPT_TERM",
                                    confidence=0.5, mention_count=1,
                                    quotes=["the Wyrm slept"]),
    }
    block = {"block_hash": "h", "prompt_sha256": "p", "system": "s",
             "user": "u", "candidate_ids": ["T:1"]}
    good = {"classifications": [{
        "id": "T:1", "category": "CREATURE_SPECIES", "confidence": 0.9,
        "evidence_quote": "the Wyrm slept"}]}
    parsed = ner_llm.parse_response(block, cands, good)
    assert len(parsed) == 1 and parsed[0].category == "CREATURE_SPECIES"

    # unknown candidate id dropped; bad category dropped; short quote dropped
    bad = {"classifications": [
        {"id": "NOPE", "category": "PERSON", "confidence": 0.5,
         "evidence_quote": "whatever long enough"},
        {"id": "T:1", "category": "NOT_A_TYPE", "confidence": 0.5,
         "evidence_quote": "the Wyrm slept"},
        {"id": "T:1", "category": "PERSON", "confidence": 0.5,
         "evidence_quote": "x"},
    ]}
    assert ner_llm.parse_response(block, cands, bad) == []
    assert ner_llm.parse_response(block, cands, None) == []
    assert ner_llm.parse_response(block, cands, [1, 2, 3]) == []


def test_merge_field_level_never_overwrites_booknlp():
    from backend.parsing.ner_llm import Classification

    llm_prop = Classification(
        candidate_id="T:1", name="the Wyrm", category="CREATURE_SPECIES",
        confidence=0.9, evidence_quote="the Wyrm slept",
        definition="un drago", schema_valid=True)

    # weak default type: LLM wins
    weak = {"entity_type": "CONCEPT_TERM", "definition": "",
            "confidence": 0.4}
    patch = ner_llm.merge_field_level(weak, llm_prop)["patch"]
    assert patch["entity_type"] == "CREATURE_SPECIES"
    assert patch["definition"] == "un drago"
    assert patch["confidence"] == round(0.5 * 0.9 + 0.5 * 0.4, 3)

    # BookNLP PERSON is never re-typed (§6.2.3: nessuna sovrascrittura)
    person = {"entity_type": "PERSON", "definition": "",
              "confidence": 0.8}
    patch = ner_llm.merge_field_level(person, llm_prop)["patch"]
    assert "entity_type" not in patch

    # user definition wins over the LLM one (§15.2)
    with_def = {"entity_type": "CONCEPT_TERM",
                "definition": "nota dell'utente", "confidence": 0.4}
    patch = ner_llm.merge_field_level(with_def, llm_prop)["patch"]
    assert "definition" not in patch


# --------------------------------------------------------------------------
# adapter: local-only gate + rate limiting + JSON errors
# --------------------------------------------------------------------------
def test_local_only_blocks_non_local_base_url():
    from backend.config import assert_local_url

    with pytest.raises(ValueError, match="local_only"):
        GatewayClient(base_url="http://api.evil-cloud.com/v1")
    with pytest.raises(ValueError, match="local_only"):
        assert_local_url("https://api.openai.com/v1/chat/completions")
    # local endpoint passes the gate
    assert GatewayClient(base_url="http://127.0.0.1:8080/v1")._base_url \
        == "http://127.0.0.1:8080/v1"


def test_rate_limiter_spaces_calls():
    limiter = RateLimiter(max_rps=4.0)
    t0 = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    elapsed = time.monotonic() - t0
    # 5 calls at 4 rps → ≥ 4 intervals = 1.0 s (with tolerance)
    assert elapsed >= 0.75, elapsed


def test_chat_json_invalid_content_raises_domain_error():
    import httpx as _httpx

    calls = {"n": 0}

    class _Resp:
        status_code = 200
        text = "not json"

        def json(self):
            raise ValueError("no json")

    def fake_request(*a, **kw):
        calls["n"] += 1
        return _Resp()

    orig = gateway_http.httpx.request
    gateway_http.httpx.request = fake_request
    try:
        client = GatewayClient(base_url="http://127.0.0.1:9/v1",
                               limiter=RateLimiter(50))
        with pytest.raises(GatewayInvalidJSON):
            client.chat_json(model="m", system_prompt="s", user_prompt="u")
    finally:
        gateway_http.httpx.request = orig
    assert calls["n"] == 1  # invalid JSON is NOT retried


# --------------------------------------------------------------------------
# API: AC3 local-only at the boundary
# --------------------------------------------------------------------------
async def test_endpoint_rejects_non_local_gateway(client, monkeypatch):
    from backend import config as cfg

    pid = (await client.post("/api/v1/projects", json={
        "title": "X", "genre_profile": "fantasy"})).json()["id"]
    monkeypatch.setattr(cfg, "LLM_GATEWAY_BASE_URL",
                        "http://api.evil-cloud.com/v1")
    r = await client.post(f"/api/v1/projects/{pid}/entities/llm-classify")
    assert r.status_code == 451
    assert "local_only" in r.json()["detail"]
    # no job was queued
    jobs = (await client.get(f"/api/v1/projects/{pid}/jobs")).json()
    assert not [j for j in jobs if j["job_type"] == "llm_classify_entities"]


# --------------------------------------------------------------------------
# end-to-end with the fake LLM: AC1 schema rate, AC2 provenance, idempotency
# --------------------------------------------------------------------------
def _booknlp_service_available() -> bool:
    """``_import_and_extract`` drives the real BookNLP GPU service (ADR-008)."""
    from backend import booknlp_service

    try:
        booknlp_service.check_health(timeout=30.0)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _booknlp_service_available(),
                    reason="BookNLP GPU service not reachable via gateway")
async def test_llm_classification_pipeline(client, fake_llm):
    pid = await _import_and_extract(client)

    r = await client.post(f"/api/v1/projects/{pid}/entities/llm-classify")
    assert r.status_code == 202, r.text
    job = await _poll_job(client, pid, r.json()["job_id"])
    assert job["status"] == "completed", job
    result = job["result"]["ner_llm"]
    assert result["candidates"] > 0
    assert result["llm_calls"] == result["blocks"]
    # AC1: every call produced schema-valid output (rate 100% ≥ 95%)
    assert result["schema_error_calls"] == 0
    assert result["schema_compliance_rate"] >= 0.95
    # temperature within §8.2 analysis band
    assert all(c["temperature"] <= 0.2 for c in fake_llm.calls)

    # AC2: provenance — LLM evidence rows exist with extractor='llm:*'
    r = await client.get(f"/api/v1/projects/{pid}/entities")
    payload = r.json()
    # the list endpoint is paginated: {"total", "page", "per_page", "entities"}
    entities = payload["entities"]
    assert payload["total"] >= len(entities)
    llm_evidence = [
        ev for e in entities
        for ev in (await client.get(
            f"/api/v1/projects/{pid}/entities/{e['id']}")).json()["evidence"]
        if ev["extractor"].startswith("llm:")
    ]
    assert llm_evidence, "no LLM-provenance evidence stored"
    for ev in llm_evidence:
        assert ev["confidence"] is not None
        assert len(ev["quote_text"]) >= 4

    # BookNLP attributions not overwritten: every PERSON is still PERSON
    for e in entities:
        if e["canonical_source"].lower() in {"kestra", "dain", "sorrel"}:
            assert e["entity_type"] == "PERSON", e

    # ---- idempotency: second run = zero new LLM calls --------------------
    calls_after_first = len(fake_llm.calls)
    r = await client.post(f"/api/v1/projects/{pid}/entities/llm-classify")
    assert r.status_code == 202, r.text
    job2 = await _poll_job(client, pid, r.json()["job_id"])
    assert job2["status"] == "completed", job2
    result2 = job2["result"]["ner_llm"]
    assert result2["cache_hits"] == result2["blocks"]
    assert result2["llm_calls"] == 0
    assert len(fake_llm.calls) == calls_after_first


@pytest.mark.skipif(not _booknlp_service_available(),
                    reason="BookNLP GPU service not reachable via gateway")
async def test_schema_errors_are_counted_not_fatal(client):
    """AC1 (negative): malformed model output cannot poison the run.

    Every block returns garbage → compliance rate 0% (proving the metric is
    real), the job still completes, and NO evidence row claims LLM provenance.
    """
    fake = _FakeLLM(bad_blocks={0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10})
    orig_chat = GatewayClient.chat_json
    orig_models = GatewayClient.list_models
    GatewayClient.chat_json = fake  # type: ignore[method-assign]
    GatewayClient.list_models = lambda self: [  # type: ignore[method-assign]
        {"id": "test-analysis-model", "display_name": "test",
         "provider": "llama-swap", "status": "loaded",
         "supports_json_schema": True, "supports_streaming": True,
         "context_window": 32768}]
    try:
        pid = await _import_and_extract(client)
        r = await client.post(
            f"/api/v1/projects/{pid}/entities/llm-classify")
        assert r.status_code == 202, r.text
        job = await _poll_job(client, pid, r.json()["job_id"])
        assert job["status"] == "completed", job
        result = job["result"]["ner_llm"]
        if result["candidates"] > 0:
            assert result["schema_valid_calls"] == 0
            assert result["schema_compliance_rate"] == 0.0
            entities = (await client.get(
                f"/api/v1/projects/{pid}/entities")).json()["entities"]
            llm_ev = [
                ev for e in entities
                for ev in (await client.get(
                    f"/api/v1/projects/{pid}/entities/{e['id']}")
                ).json()["evidence"]
                if ev["extractor"].startswith("llm:")]
            assert llm_ev == []
    finally:
        GatewayClient.chat_json = orig_chat  # type: ignore[method-assign]
        GatewayClient.list_models = orig_models  # type: ignore[method-assign]


# --------------------------------------------------------------------------
# live Gateway (skipped when the proxy is down) — the §8 contract for real
# --------------------------------------------------------------------------
def _gateway_has_models() -> bool:
    import httpx

    try:
        r = httpx.get("http://127.0.0.1:8080/v1/models", timeout=3)
        # HTTP 200 with an empty model list (proxy up, nothing loaded) is not
        # enough to run the live contract test — it needs at least one model.
        return r.status_code == 200 and bool(r.json().get("data"))
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _gateway_has_models(), reason="Gateway not reachable or no models loaded")
async def test_live_gateway_json_contract(client):
    """The real proxy answers a schema-constrained classification call.

    Model selection mirrors production (_resolve_model): text-capable rows
    only, a currently ``loaded`` one preferred — the first listed model can
    be a backend that fails to load (llama-swap routes TTS/embeddings too).
    """
    from backend.llm_ner_runner import _is_text_generation_model

    client_live = GatewayClient(base_url="http://127.0.0.1:8080/v1")
    models = client_live.list_models()
    assert models, "Gateway exposes no models"
    text_models = [
        m for m in models
        if _is_text_generation_model(m["id"], m["display_name"])]
    pool = text_models or models
    ready = [m for m in pool if m["status"] == "loaded"] or pool
    out = client_live.chat_json(
        model=ready[0]["id"],
        system_prompt='You output only JSON: {"ok": true}',
        user_prompt='Return {"ok": true}.',
        temperature=0.0,
    )
    assert out.get("ok") is True
