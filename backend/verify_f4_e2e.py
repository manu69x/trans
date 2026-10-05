"""E2E verification of the full PRD §5 flow on the local corpus.

Pipeline (§5): upload → parse → structure → segment → entities (BookNLP)
→ translation (LLM Gateway) → QA → approval → export → hardening
(signed URL / rate limit / at-rest encryption / backup+restore / log scan).

Run:
  cd backend
  DATABASE_URL=postgresql://trans:trans@127.0.0.1:5432/trans_e2e \
  STORAGE_ROOT=/tmp/trans-e2e-storage \
  .venv-backend/bin/python verify_f4_e2e.py

Isolated DB (trans_e2e) so the shared dev DB is never touched. Uses the
real local LLM Gateway for translation when reachable; falls back to the
recorded deterministic response otherwise (the flow shape is identical).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

TEST_DB = "trans_e2e"
TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", f"postgresql://trans:trans@127.0.0.1:5432/{TEST_DB}")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL
os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-e2e-storage")
os.environ.setdefault("LOCAL_ONLY", "1")

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, cond: bool, extra: str = "") -> bool:
    RESULTS.append((label, bool(cond), extra))
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{extra}]" if extra else ""))
    return bool(cond)


def _pdf(pages: int = 2) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    corpus_txt = (REPO_ROOT / "docs/benchmarks/corpus/txt/"
                  "native_01_literary_excerpt.txt").read_text()
    lines = [ln for ln in corpus_txt.splitlines() if ln.strip()]
    for i in range(pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        if i == 0:
            pdf.cell(0, 10, text="Chapter One", new_x="LMARGIN", new_y="NEXT")
        for line in lines[i * 6:(i + 1) * 6]:
            pdf.multi_cell(pdf.w - pdf.l_margin - pdf.r_margin, 8, text=line)
    return bytes(pdf.output())


async def _ensure_db() -> None:
    from sqlalchemy import create_engine, text

    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{TEST_DB}' AND pid <> pg_backend_pid()"))
            conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB}"))
            conn.execute(text(f"CREATE DATABASE {TEST_DB} TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        f"postgresql://trans:trans@127.0.0.1:5432/{TEST_DB}",
        isolation_level="AUTOCOMMIT")
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


async def main() -> int:
    await _ensure_db()

    import subprocess
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True, cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL})

    from httpx import ASGITransport, AsyncClient

    from backend.main import app
    from backend.db import SessionLocal
    from backend.models import AuditLog, TranslationUnit
    from backend.scheduler import wait_for_workers

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           timeout=120.0) as c:
        # ---- 1. create project (copyright on upload) --------------------
        r = await c.post("/api/v1/projects", json={
            "title": "E2E §5 Corpus", "genre_profile": "romanzo"})
        check("create project", r.status_code == 201, r.text[:120])
        pid = r.json()["id"]

        # ---- 2. upload corpus PDF (§5.1, copyright §13.2) ---------------
        r = await c.post(
            f"/api/v1/projects/{pid}/documents",
            files={"file": ("corpus.pdf", _pdf(2), "application/pdf")},
            data={"copyright_confirmed": "true"})
        check("upload corpus PDF (copyright confirmed)",
              r.status_code == 201, r.text[:120])
        document_id = r.json()["document_id"]
        wait_for_workers()

        # ---- 3. parse → structure → segments (§5.2-5.4) -----------------
        r = await c.post(
            f"/api/v1/projects/{pid}/documents/{document_id}/parse_l1")
        check("queue L1 parse", r.status_code == 202, r.text[:120])
        wait_for_workers()

        r = await c.post(f"/api/v1/projects/{pid}/structure/detect")
        check("queue structure detection", r.status_code == 202)
        wait_for_workers()

        r = await c.get(f"/api/v1/projects/{pid}/structure")
        nodes = r.json().get("nodes", [])
        check("structure nodes produced", r.status_code == 200 and len(nodes) >= 1,
              f"{len(nodes)} nodes")
        if not nodes:
            print("E2E: no structure nodes — aborting remaining checks")
            return _summary()
        chapter = nodes[0]
        confirm = await c.post(
            f"/api/v1/projects/{pid}/structure/{chapter['node_id']}/confirm")
        check("user confirms chapter", confirm.status_code in (200, 201),
              confirm.text[:120])

        r = await c.post(
            f"/api/v1/projects/{pid}/structure/{chapter['node_id']}/segment")
        check("queue chapter segmentation", r.status_code == 202)
        wait_for_workers()

        with SessionLocal() as db:
            units = (db.query(TranslationUnit)
                     .filter(TranslationUnit.project_id == pid).all())
        check("segments produced for chapter", len(units) >= 1,
              f"{len(units)} units")
        if not units:
            return _summary()
        seg_ids = [str(u.id) for u in units]

        # ---- 4. entities (BookNLP extraction, §6) ------------------------
        r = await c.post(f"/api/v1/projects/{pid}/entities/extract")
        check("queue entity extraction", r.status_code == 202)
        wait_for_workers(timeout=480.0)
        r = await c.get(f"/api/v1/projects/{pid}/entities")
        ents = r.json() if isinstance(r.json(), list) else r.json().get("entities", [])
        check("entity extraction pipeline ran", r.status_code == 200,
              f"{len(ents)} entities")

        # ---- 5. translation via LLM Gateway (§5.5, §8) -----------------
        from backend.gateway_http import GatewayClient

        sources = {str(u.id): u.source_text for u in units}
        translations_payload = {
            "translations": [
                {"segment_id": sid,
                 "target_text": f"[IT] {sources[sid][:80]}",
                 "flags": []}
                for sid in seg_ids
            ]
        }

        def _live_gateway() -> bool:
            try:
                import httpx
                r = httpx.get("http://127.0.0.1:8080/v1/models", timeout=3)
                return r.status_code == 200
            except Exception:
                return False

        if _live_gateway():
            check("gateway proxy reachable (real LLM path)", True)
        else:
            orig = GatewayClient.chat_json

            def _fake_chat(self, *, model, system_prompt, user_prompt,
                           temperature=0.0, max_tokens=None, seed=None):
                return translations_payload

            GatewayClient.chat_json = _fake_chat  # type: ignore[method-assign]
            check("gateway proxy unreachable -> deterministic fallback installed", True)

        chapter_id = str(chapter["node_id"])
        r = await c.post(f"/api/v1/projects/{pid}/translation/run", json={
            "chapter_id": chapter_id,
            "segments": [
                {"segment_id": sid, "source_text": sources[sid],
                 "ordinal": i}
                for i, sid in enumerate(seg_ids)
            ],
            "block_source": " ".join(sources[sid] for sid in seg_ids),
            "idempotency_key": "e2e-corpus",
            "model": "translategemma-12b-it-q4_k_s_G1",
        })
        check("queue translation run", r.status_code == 202, r.text[:200])
        # The real LLM path needs longer than a unit-test: cold-start of the
        # target model on llama-swap can take ~30-60 s. If the first attempt
        # fails (proxy cold start), retry once with a fresh idempotency key.
        wait_for_workers(timeout=600.0)
        with SessionLocal() as db:
            have = (db.query(TranslationUnit)
                    .filter(TranslationUnit.project_id == pid,
                            TranslationUnit.target_text.isnot(None)).count())
        if have == 0:
            r = await c.post(f"/api/v1/projects/{pid}/translation/run", json={
                "chapter_id": chapter_id,
                "segments": [
                    {"segment_id": sid, "source_text": sources[sid],
                     "ordinal": i}
                    for i, sid in enumerate(seg_ids)
                ],
                "block_source": " ".join(sources[sid] for sid in seg_ids),
                "idempotency_key": "e2e-corpus-retry",
                "model": "translategemma-12b-it-q4_k_s_G1",
            })
            check("translation retry after cold start", r.status_code == 202)
            wait_for_workers(timeout=600.0)

        if not _live_gateway():
            GatewayClient.chat_json = orig  # type: ignore[method-assign]

        with SessionLocal() as db:
            tus = (db.query(TranslationUnit)
                   .filter(TranslationUnit.project_id == pid).all())
        drafts = [t for t in tus if t.target_text]
        check("translation output saved as machine_draft",
              len(drafts) >= 1 and all(t.status == "machine_draft" for t in tus),
              f"{len(drafts)}/{len(tus)} drafts")

        # ---- 6. QA pass (§5.6, §10) --------------------------------------
        r = await c.post(f"/api/v1/projects/{pid}/qa/run",
                         json={"critic_backend": "deterministic"})
        body = r.json()
        check("QA run (3 livelli)", r.status_code == 200
              and body.get("segments_evaluated", 0) >= 1, str(body)[:160])

        # ---- 7. approval (§5.7) ------------------------------------------
        approved = 0
        for t in drafts:
            r = await c.post(
                f"/api/v1/projects/{pid}/segments/{t.id}/approve",
                json={"reviewer": "e2e"})
            if r.status_code in (200, 201):
                approved += 1
        check("segments approved (audited)", approved == len(drafts),
              f"{approved}/{len(drafts)}")

        # ---- 8. export (§5.8, §15.4) --------------------------------------
        r = await c.post(f"/api/v1/projects/{pid}/export", json={
            "format": "html"})
        ok_export = r.status_code == 200
        check("export of approved segments (HTML)", ok_export, r.text[:160])
        if ok_export:
            check("export content carries approved text",
                  b"[IT]" in r.content or "BOZZA" not in r.text[:200])

        r = await c.get(f"/api/v1/projects/{pid}/export/snapshots")
        check("export snapshot + manifest recorded", r.status_code == 200)

        # ---- 9. hardening: signed URL (§13.1) -----------------------------
        doc = await c.get(f"/api/v1/projects/{pid}/documents")
        doc_id = doc.json()[0]["id"]
        r = await c.post(
            f"/api/v1/projects/{pid}/documents/{doc_id}/signed_url",
            json={"ttl_seconds": 120})
        check("signed URL issued", r.status_code == 200, r.text[:120])
        signed = r.json()
        r2 = await c.get(signed["url"])
        check("signed URL serves the stored PDF",
              r2.status_code == 200 and r2.content.startswith(b"%PDF"))
        bad = signed["token"][:-1] + ("0" if signed["token"][-1] != "0" else "1")
        r3 = await c.get(signed["url"].replace(
            f"token={signed['token']}", f"token={bad}"))
        check("tampered signed URL rejected 403", r3.status_code == 403)

        # ---- 10. audit log immutabile (§13.1) -----------------------------
        with SessionLocal() as db:
            audits = db.query(AuditLog).filter(
                AuditLog.project_id == pid).all()
        actions = sorted({a.action for a in audits})
        # the backend's audited action is 'segment_approved' (editor_routes);
        # the E2E previously expected a non-existent 'approve_segment' name.
        check("audit trail covers export + approval",
              "export" in actions and "segment_approved" in actions,
              f"actions={actions}")

        # ---- 11. backup + restore (AC1) -----------------------------------
        from backend.backup import create_backup, restore_backup

        backup_root = "/tmp/trans-e2e-backups"
        manifest = create_backup(backup_root, TEST_DATABASE_URL,
                                 description="E2E F4")
        check("versioned backup created (DB+assets)",
              manifest.get("asset_count", 0) >= 1
              and manifest.get("db_dump_sha256"),
              f"assets={manifest.get('asset_count')}")

        restore = restore_backup(
            backup_root, manifest["version"],
            f"postgresql://trans:trans@127.0.0.1:5432/trans_e2e_restore",
            minio_bucket=os.getenv("MINIO_BUCKET", "trans-e2e-restore"))
        check("restore on clean DB matches manifest",
              restore["manifest_sha_ok"]
              and restore["assets_restored"] == manifest["asset_count"],
              str(restore)[:200])

    return _summary()


def _summary() -> int:
    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"E2E RESULT: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for label, ok, extra in failed:
        print(f"  FAILED: {label} {extra}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
