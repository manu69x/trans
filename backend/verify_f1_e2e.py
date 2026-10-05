"""F1 end-to-end verification against a REAL live backend + real MinIO.

Runs against uvicorn on 127.0.0.1:8000 (Postgres 18 + MinIO server on
127.0.0.1:9000). Verifies the three acceptance criteria of task t_5da2b338:

AC1. upload ~100MB PDF -> 201, sha256/size/page_count stored, MinIO object
     retrievable with identical bytes, dedup by hash works (no 2nd object);
AC2. project status follows the PRD 5.1 machine (DRAFT -> IMPORTING -> PARSED,
     invalid transitions rejected);
AC3. copyright declaration mandatory on first upload (403 without it) and the
     confirmation + the upload are recorded in the immutable audit log.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
ok = 0
fail = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}  {extra}")


def make_pdf(n_pages: int) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for i in range(n_pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, text=f"Verification page {i + 1}")
    return bytes(pdf.output())


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=120.0)

    # --- health ------------------------------------------------------------
    r = c.get("/../health")  # httpx collapses to /api/health -> 404; use root
    r = httpx.get("http://127.0.0.1:8000/health", timeout=10)
    check("backend /health is ok", r.status_code == 200 and r.json()["status"] == "ok")
    r = httpx.get("http://127.0.0.1:8000/health/db", timeout=10)
    check("backend /health/db is ok (postgres+pgvector)", r.status_code == 200)

    # --- AC1: 100MB upload, metadata, storage, dedup ------------------------
    print("\nAC1: upload ~100MB PDF, hash/metadata, object storage, dedup")
    r = c.post("/projects", json={"title": "Ver F1 big", "genre_profile": "saga"})
    check("POST /projects -> 201 (DRAFT)", r.status_code == 201)
    pid = r.json()["id"]
    check("initial status is DRAFT", r.json()["status"] == "DRAFT", r.text)

    # A valid multi-page PDF padded to >100 MB with a post-header padding
    # comment object, so pypdf can still count pages on it.
    pdf = make_pdf(4)
    padding = b"\n%" + b"F1verifypadding.\n" * 1  # placeholder, replaced below
    # build a large comment stream between header and body is fiddly; simpler:
    # append a huge unused xref-ish comment at the end (PDF readers tolerate
    # trailing data after %%EOF).
    trailer = b"\n% " + b"F" * (100 * 1024 * 1024) + b"\n"
    big = pdf + trailer
    size_mb = len(big) / (1024 * 1024)
    print(f"  payload: {len(big)} bytes ({size_mb:.1f} MB)")

    files = {"file": ("big-book.pdf", big, "application/pdf")}
    r = c.post(f"/projects/{pid}/documents",
               files=files, data={"copyright_confirmed": "true"})
    check("POST /projects/{id}/documents (100MB) -> 201", r.status_code == 201, r.text)
    body = r.json()
    expected_sha = hashlib.sha256(big).hexdigest()
    check("sha256 matches computed hash", body.get("sha256") == expected_sha,
          f"{body.get('sha256')} != {expected_sha}")
    check("size_bytes == payload size", body.get("size_bytes") == len(big))
    check("page_count == 4 (pypdf)", body.get("page_count") == 4,
          str(body.get("page_count")))
    check("storage_key is <project>/docs/<uuid>.pdf",
          body.get("storage_key", "").startswith(f"{pid}/docs/"))

    # MinIO holds the exact bytes (verify with the S3 API directly)
    from minio import Minio

    mc = Minio("127.0.0.1:9000", access_key="minioadmin",
               secret_key="minioadmin", secure=False)
    check("minio reachable and bucket exists", mc.bucket_exists("trans"))
    key = body["storage_key"]
    stat = mc.stat_object("trans", key)
    check("minio object size matches", stat.size == len(big),
          f"{stat.size} != {len(big)}")
    resp = mc.get_object("trans", key)
    try:
        stored = resp.read()
    finally:
        resp.close()
        resp.release_conn()
    check("minio object bytes round-trip identical",
          hashlib.sha256(stored).hexdigest() == expected_sha)

    # DB metadata row (direct Postgres read, not via API). The parse job runs
    # on a background thread and chewing through 100 MB takes a few seconds,
    # so poll until the document row settles on parsed/<page_count>.
    import subprocess
    import time

    env = dict(os.environ, PGPASSWORD="trans")

    def psql(query: str) -> tuple[int, str]:
        out = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-U", "trans", "-d", "trans", "-tAc", query],
            capture_output=True, text=True, env=env, timeout=30,
        )
        return out.returncode, out.stdout.strip()

    expected_row = f"{len(big)}|{expected_sha}|4|parsed"
    row = ""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        _, row = psql("SELECT size_bytes, sha256, page_count, status FROM "
                      "documents WHERE id = '%s'" % body["document_id"])
        if row == expected_row:
            break
        time.sleep(2)
    check("documents row settles to size|sha|pages|status=parsed",
          row == expected_row, row)

    # dedup: same bytes again -> duplicate, no new object
    n_before = sum(1 for _ in mc.list_objects("trans", prefix=f"{pid}/docs/"))
    r2 = c.post(f"/projects/{pid}/documents",
                files={"file": ("big-book-again.pdf", big, "application/pdf")},
                data={"copyright_confirmed": "true"})
    check("re-upload identical bytes -> 201 duplicate", r2.status_code == 201
          and r2.json().get("status") == "duplicate", r2.text)
    check("duplicate returns same document_id",
          r2.json().get("document_id") == body["document_id"])
    n_after = sum(1 for _ in mc.list_objects("trans", prefix=f"{pid}/docs/"))
    check("no extra object stored after dedup", n_before == n_after == 1,
          f"{n_before} -> {n_after}")

    # AC2: state machine PRD 5.1 ----------------------------------------
    print("\nAC2: project status follows the 5.1 state machine")
    q = "SELECT status FROM projects WHERE id = '%s'" % pid
    _, st = psql(q)
    check("after parse the project is PARSED (DRAFT->IMPORTING->PARSED)",
          st == "PARSED", st)

    # PATCH cannot teleport the state machine
    r = c.patch(f"/projects/{pid}", json={"status": "EXPORTED"})
    check("PATCH status is not a bypass (400/ignored)", r.status_code == 400,
          f"{r.status_code} {r.text}")
    _, st = psql(q)
    check("status still PARSED after illegal PATCH", st == "PARSED", st)

    # invalid service-level transition is rejected (fresh DRAFT project)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("DATABASE_URL",
                          "postgresql://trans:trans@127.0.0.1:5432/trans")
    from backend.db import SessionLocal
    from backend.models import Project
    from backend.service import ProjectService, StateError, StateMachine

    db = SessionLocal()
    try:
        svc = ProjectService(db)
        p = svc.create_project("smoke-state-machine", "saga")
        raised = False
        try:
            svc.transition(p.id, "EXPORTED")  # DRAFT -> EXPORTED is illegal
        except StateError:
            raised = True
        check("DRAFT -> EXPORTED raises StateError", raised)
        check("state stays DRAFT after rejected transition",
              db.get(Project, p.id).status == "DRAFT")
    finally:
        db.close()
    check("EXPORTED is terminal (no forward transition)",
          not any(StateMachine.can("EXPORTED", s) for s in
                  ["DRAFT", "IMPORTING", "PARSED", "APPROVED"]))

    # --- AC3: copyright + audit --------------------------------------------
    print("\nAC3: copyright declaration mandatory + audit trail")
    r = c.post("/projects", json={"title": "Ver F1 copy", "genre_profile": "horror"})
    pid2 = r.json()["id"]
    small = make_pdf(2)
    r = c.post(f"/projects/{pid2}/documents",
               files={"file": ("nc.pdf", small, "application/pdf")},
               data={"copyright_confirmed": "false"})
    check("upload without copyright confirmation -> 403", r.status_code == 403,
          f"{r.status_code} {r.text}")

    r = c.post(f"/projects/{pid2}/documents",
               files={"file": ("nc.pdf", small, "application/pdf")},
               data={"copyright_confirmed": "true"})
    check("upload with confirmation -> 201", r.status_code == 201, r.text)
    q = "SELECT copyright_confirmed FROM projects WHERE id = '%s'" % pid2
    _, flag = psql(q)
    check("projects.copyright_confirmed = true in DB", flag == "t", flag)

    q = ("SELECT action FROM audit_log WHERE project_id = '%s' ORDER BY created_at"
         % pid2)
    _, actions_raw = psql(q)
    actions = [a for a in actions_raw.splitlines() if a]
    print("  audit actions:", actions)
    check("audit has copyright_confirmed", "copyright_confirmed" in actions)
    check("audit has document_uploaded", "document_uploaded" in actions)
    check("audit has project_created", "project_created" in actions)

    # audit log is append-only: UPDATE/DELETE must be rejected by the DB
    rc, _ = psql("UPDATE audit_log SET action = 'tampered' "
                 "WHERE project_id = '%s'" % pid2)
    check("audit UPDATE rejected (append-only)", rc != 0)
    rc, _ = psql("DELETE FROM audit_log WHERE project_id = '%s'" % pid2)
    check("audit DELETE rejected (append-only)", rc != 0)

    # jobs endpoint
    r = c.get(f"/projects/{pid}/jobs")
    check("GET /projects/{id}/jobs -> 200 with parse job",
          r.status_code == 200 and len(r.json()) == 1
          and r.json()[0]["job_type"] == "parse"
          and r.json()[0]["status"] == "completed", r.text[:200])

    print(f"\nRESULT: {ok} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
