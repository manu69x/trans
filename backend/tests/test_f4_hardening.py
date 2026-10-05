"""F4 hardening tests (PRD §13/§14, ).

* AC-signed  -- signed download URLs: valid token serves bytes, expired /
  tampered / wrong-resource tokens answer 403 (§13.1).
* AC-rate    -- per-IP token bucket: burst exhaustion answers 429 with a
  Retry-After header, another IP is unaffected (§13/§14).
* AC-encrypt -- at-rest encryption: with a storage key file configured the
  object on disk is NOT plaintext; reads still return the original bytes
  (transparent envelope), and without a key files stay plaintext.
* AC-retain  -- retention + secure delete: expired keys are purged with
  overwrite-before-unlink; retention=0 keeps everything.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

# Isolated test DB decided BEFORE backend.main is imported (same pattern as
# test_f4_qa.py / test_f4_export.py).
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_hard",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ["STORAGE_ROOT"] = "/tmp/trans-test-hard-storage"
os.environ.setdefault("LOCAL_ONLY", "1")


def _ensure_test_db() -> None:
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_hard' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_hard"))
            conn.execute(text(
                "CREATE DATABASE trans_hard TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_hard",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


_ensure_test_db()

from backend.main import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402
from backend.rate_limit import limiter as rate_limiter  # noqa: E402
from backend.storage import get_storage_provider  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    import subprocess

    engine.dispose()
    _ensure_test_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True, cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL},
    )
    engine.dispose()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    rate_limiter.reset()
    engine.dispose()
    yield
    rate_limiter.reset()
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _pdf(pages: int = 1) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for i in range(pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        pdf.cell(0, 10, text=f"Hardening page {i + 1}")
    return bytes(pdf.output())


async def _project_with_doc(client: AsyncClient) -> tuple[str, str]:
    r = await client.post("/api/v1/projects", json={
        "title": "Hardening Book", "genre_profile": "saggio"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("hard.pdf", _pdf(1), "application/pdf")},
        data={"copyright_confirmed": "true"})
    assert r.status_code == 201, r.text
    return pid, r.json()["document_id"]


# ---------------------------------------------------------------------------
# AC-signed: signed download URLs (§13.1)
# ---------------------------------------------------------------------------
async def test_signed_url_valid_token_serves_bytes(client):
    pid, did = await _project_with_doc(client)
    r = await client.post(
        f"/api/v1/projects/{pid}/documents/{did}/signed_url",
        json={"ttl_seconds": 120})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] and body["exp"] > time.time()
    # follow the signed link
    r2 = await client.get(body["url"])
    assert r2.status_code == 200, r2.text
    assert r2.content.startswith(b"%PDF")


async def test_signed_url_rejects_tampered_expired_and_foreign(client):
    from backend import signed_urls

    pid, did = await _project_with_doc(client)
    resource = f"projects/{pid}/documents/{did}/file"
    # tampered token
    signed = signed_urls.sign_url(resource, ttl_seconds=60)
    bad = signed["token"][:-1] + ("0" if signed["token"][-1] != "0" else "1")
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{did}/file",
        params={"exp": signed["exp"], "token": bad})
    assert r.status_code == 403, r.text
    # expired token
    expired = signed_urls.sign_url(resource, ttl_seconds=60,
                                   now=time.time() - 3600)
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{did}/file",
        params={"exp": expired["exp"], "token": expired["token"]})
    assert r.status_code == 403
    # valid signature but bound to a different resource
    other = signed_urls.sign_url("projects/x/documents/y/file", ttl_seconds=60)
    r = await client.get(
        f"/api/v1/projects/{pid}/documents/{did}/file",
        params={"exp": other["exp"], "token": other["token"]})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# AC-rate: per-IP rate limiting (§13/§14)
# ---------------------------------------------------------------------------
async def test_login_rate_limited_per_ip(client):
    rate_limiter.limits["auth"] = (2.0, 0.05)  # burst 2, slow refill
    rate_limiter.reset()
    payload = {"email": "nobody@example.com", "password": "wrong"}
    codes = set()
    for _ in range(4):
        r = await client.post("/api/v1/auth/login", json=payload)
        codes.add(r.status_code)
    assert 401 in codes and 429 in codes, codes
    # Retry-After present on the 429
    r = await client.post("/api/v1/auth/login", json=payload)
    if r.status_code == 429:
        assert "retry-after" in {k.lower() for k in r.headers.keys()}
    # a different client IP still has budget
    r = await client.post("/api/v1/auth/login", json=payload,
                          headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401  # not 429


async def test_upload_rate_limit_exhaustion(client):
    rate_limiter.limits["upload"] = (1.0, 0.01)
    rate_limiter.reset()
    pid, _ = await _project_with_doc(client)  # consumes the 1 burst token
    r = await client.post(
        f"/api/v1/projects/{pid}/documents",
        files={"file": ("b.pdf", _pdf(1), "application/pdf")},
        data={"copyright_confirmed": "true"},
        headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code == 429, r.text
    assert "retry-after" in {k.lower() for k in r.headers.keys()}


# ---------------------------------------------------------------------------
# AC-encrypt: at-rest storage encryption (§13.1)
# ---------------------------------------------------------------------------
async def test_storage_encrypted_at_rest_and_transparent_read(client, tmp_path):
    key_file = tmp_path / "storage.key"
    from backend import crypto_at_rest

    crypto_at_rest.generate_key_file(str(key_file))
    os.environ["TRANS_STORAGE_KEY_FILE"] = str(key_file)
    try:
        # force a fresh provider under the configured root
        os.environ["STORAGE_ROOT"] = str(tmp_path / "storage")
        provider = get_storage_provider()
        payload = b"%PDF-1.7 manoscritto riservato " + bytes(range(256))
        provider.put("enc/test.bin", payload)
        # on disk the plaintext is gone
        on_disk = (Path(os.environ["STORAGE_ROOT"]) / "enc" / "test.bin").read_bytes()
        assert on_disk != payload
        assert crypto_at_rest.is_encrypted(on_disk)
        assert b"manoscritto" not in on_disk
        # read path is transparent
        assert provider.get("enc/test.bin") == payload
    finally:
        os.environ.pop("TRANS_STORAGE_KEY_FILE", None)

    # without a key: plaintext passthrough (dev mode)
    provider2 = get_storage_provider()
    provider2.put("enc/plain.bin", b"PLAINTEXT-OK")
    on_disk2 = (Path(os.environ["STORAGE_ROOT"]) / "enc" / "plain.bin").read_bytes()
    assert on_disk2 == b"PLAINTEXT-OK"
    assert provider2.get("enc/plain.bin") == b"PLAINTEXT-OK"


# ---------------------------------------------------------------------------
# AC-retain: retention + secure delete (§13.1)
# ---------------------------------------------------------------------------
async def test_retention_purge_and_secure_delete():
    from backend import retention as rt

    provider = get_storage_provider()
    old_key, keep_key = "ret/old.bin", "ret/keep.bin"
    provider.put(old_key, b"OLD-ASSET" * 100)
    provider.put(keep_key, b"KEEP-ASSET")
    now = time.time()
    expired = rt.find_expired(
        [(old_key, now - 30 * 86400), (keep_key, now)], days=7)
    assert expired == [old_key]
    purged = rt.purge_expired(
        provider, [(old_key, now - 30 * 86400), (keep_key, now)], days=7)
    assert purged == [old_key]
    assert not provider.exists(old_key)      # gone
    assert provider.exists(keep_key)         # window not reached
    assert provider.get(keep_key) == b"KEEP-ASSET"

    # secure delete wipes the file at the filesystem level too
    p = Path(os.getenv("STORAGE_ROOT", "/tmp")) / "ret" / "sd.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"SUPER-SEGRETO" * 500)
    assert rt.secure_delete_file(p)
    assert not p.exists()

    # provider-level delete exists on both implementations
    provider.delete(keep_key)
    assert not provider.exists(keep_key)

    # retention window 0 => nothing is ever expired
    assert rt.find_expired([(old_key, 0)], days=0) == []
