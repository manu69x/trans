#!/usr/bin/env python3
"""Benchmark NER LLM (task t_e87a4040) — capitolo fantasy del corpus.

Runs the full pipeline IN-PROCESS against the real LLM Gateway
(ADR-001: http://127.0.0.1:8080/v1) on the fantasy chapter:

  import → parse_l1 → structure detect → BookNLP+NER extraction
  → LLM classification of §6.2.3 candidates

Measures (task body: "% candidati classificati, precision su campione"):
  * candidate selection: how many of the proposed entities enter the
    §6.2.3 LLM scope;
  * schema compliance: valid calls / total calls (AC1, ≥ 95%);
  * per-candidate classification table against the hand-annotated gold
    types, with precision over the classified sample (AC2 grounding);
  * idempotency: a second pass must cost zero LLM calls.

The full-DB pipeline runs against a THROWAWAY Postgres database
(``trans_test`` — the ``vector`` extension is superuser-only on this
box, and ``trans_test`` is the pgvector-capable database that is wiped
and rebuilt on every run). Output: JSON to
docs/benchmarks/results-ner-llm.json + this script's stdout.

Usage:  backend/.venv-backend/bin/python docs/benchmarks/bench_ner_llm.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND))

os.environ["DATABASE_URL"] = os.environ.get(
    "BENCH_DATABASE_URL", "postgresql://trans:trans@127.0.0.1:5432/trans_test")
os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-bench-storage-nerllm")
os.environ.setdefault("LOCAL_ONLY", "1")

import httpx  # noqa: E402

GOLD = {
    # canonical lower name → gold §6.3 category (hand-annotated)
    "the withering": "CURSE",
    "the wyrm": "CREATURE_SPECIES",
    "the wyrmlings": "CREATURE_SPECIES",
    "the black key": "OBJECT_ARTIFACT",
    "the order of the pale hand": "ORG_FACTION",
    "kestra": "PERSON",
    "dain": "PERSON",
    "sorrel": "PERSON",
    "ravenwood manor": "LOCATION",
    "thornbury": "LOCATION",
}


async def wait_job(c: httpx.AsyncClient, pid: str, job_id: str,
                   timeout: float = 900.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        jobs = (await c.get(f"/api/v1/projects/{pid}/jobs")).json()
        job = next(j for j in jobs if j["id"] == job_id)
        if job["status"] in ("completed", "failed"):
            if job["status"] == "failed":
                raise RuntimeError(f"job failed: {job.get('error')}")
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(job)
        await asyncio.sleep(2.0)


async def _drain(c: httpx.AsyncClient) -> None:
    """Join the in-process scheduler threads (import/structure jobs)."""
    from backend.scheduler import wait_for_workers

    wait_for_workers(timeout=300.0)


async def _amain() -> int:
    from backend.db import Base, engine
    from backend.main import app

    # throwaway tables: drop/recreate each table but NEVER the schema —
    # dropping `public` would remove the pgvector type, which only a
    # superuser can reinstall on this box (trans is not superuser).
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=120.0) as c:
        t0 = time.monotonic()
        pid = (await c.post("/api/v1/projects", json={
            "title": "Bench NER LLM", "genre_profile": "fantasy"})).json()["id"]
        pdf = (REPO / "docs" / "benchmarks" / "corpus"
               / "native_05_fantasy_chapter.pdf").read_bytes()
        r = await c.post(f"/api/v1/projects/{pid}/documents",
                         files={"file": ("fantasy.pdf", pdf,
                                         "application/pdf")},
                         data={"copyright_confirmed": "true"})
        doc_id = r.json()["document_id"]
        r = await c.post(
            f"/api/v1/projects/{pid}/documents/{doc_id}/parse_l1")
        r.raise_for_status()
        await _drain(c)
        r = await c.post(f"/api/v1/projects/{pid}/structure/detect")
        r.raise_for_status()
        await _drain(c)
        job = (await c.post(
            f"/api/v1/projects/{pid}/entities/extract")).json()
        await wait_job(c, pid, job["job_id"], timeout=1200.0)
        import_seconds = time.monotonic() - t0

        # entities after the deterministic pass
        entities = (await c.get(f"/api/v1/projects/{pid}/entities")).json()
        before = {e["canonical_source"]: e for e in entities}

        # ---- LLM classification against the REAL proxy -------------------
        t1 = time.monotonic()
        r = await c.post(f"/api/v1/projects/{pid}/entities/llm-classify")
        r.raise_for_status()
        job = await wait_job(c, pid, r.json()["job_id"], timeout=1200.0)
        llm_seconds = time.monotonic() - t1
        result = job["result"]["ner_llm"]

        entities_after = (await c.get(
            f"/api/v1/projects/{pid}/entities")).json()

        # precision sample: per gold entity, what did the pipeline say?
        rows = []
        for name_lower, gold_type in GOLD.items():
            row = {
                "name": name_lower, "gold": gold_type,
                "proposed": None, "final": None,
                "llm_evidence": 0,
            }
            for key, e in before.items():
                if (key.lower() == name_lower or name_lower in key.lower()
                        or key.lower() in name_lower):
                    row["proposed"] = e["entity_type"]
            for key, e in {x["canonical_source"]: x
                           for x in entities_after}.items():
                if (key.lower() == name_lower or name_lower in key.lower()
                        or key.lower() in name_lower):
                    row["final"] = e["entity_type"]
            rows.append(row)

        # llm evidence per gold entity (provenance, AC2)
        for row in rows:
            matches = [e for e in entities_after
                       if row["name"] in e["canonical_source"].lower()
                       or e["canonical_source"].lower() in row["name"]]
            for e in matches:
                detail = (await c.get(
                    f"/api/v1/projects/{pid}/entities/{e['id']}")).json()
                row["llm_evidence"] += sum(
                    1 for ev in detail["evidence"]
                    if ev["extractor"].startswith("llm:"))

        # precision on the classified sample (final type == gold)
        scored = [row for row in rows if row["final"] is not None]
        correct = [row for row in scored if row["final"] == row["gold"]]
        precision = (len(correct) / len(scored)) if scored else None

        # idempotency: second pass, expect zero calls
        r = await c.post(f"/api/v1/projects/{pid}/entities/llm-classify")
        r.raise_for_status()
        job2 = await wait_job(c, pid, r.json()["job_id"], timeout=600.0)
        rerun = job2["result"]["ner_llm"]

        report = {
            "chapter": "native_05_fantasy_chapter",
            "model": result["model"],
            "candidates_selected": result["candidates"],
            "entities_proposed_total": len(entities),
            "blocks": result["blocks"],
            "llm_calls": result["llm_calls"],
            "schema_valid_calls": result["schema_valid_calls"],
            "schema_error_calls": result["schema_error_calls"],
            "schema_compliance_rate": result["schema_compliance_rate"],
            "classifications": result["classifications"],
            "classified_entities": result["classified"],
            "import_and_booknlp_seconds": round(import_seconds, 1),
            "llm_seconds": round(llm_seconds, 1),
            "gold_sample": rows,
            "precision_on_sample": precision,
            "precision_n": len(scored),
            "idempotent_rerun": {
                "llm_calls": rerun["llm_calls"],
                "cache_hits": rerun["cache_hits"],
            },
        }
        out = Path(__file__).with_name("results-ner-llm.json")
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
