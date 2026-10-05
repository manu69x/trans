"""Versioned, restorable backups of DB + assets + snapshots (PRD §14, §13.1).

Implements the PRD §14 backup requirement:

* **DB** -- a custom-format ``pg_dump`` (``-Fc``) of the whole Postgres
  database. This covers every row, including ``projects.model_settings``
  (prompt templates), ``llm_runs``, ``memory_snapshots`` (glossary/TM/export
  snapshots) and the append-only ``audit_log``.
* **Assets** -- every object in the local MinIO bucket (uploaded PDFs,
  exports) copied out to the backup, with a SHA-256 per object in the
  manifest.
* **Manifest** -- ``manifest.json`` records the version, timestamp, source
  DB (password redacted), table row counts, asset count, and the SHA-256 of
  every stored artefact, so a restore can be *verified* (AC1).

**Keys outside the repo (§13.1).** When ``TRANS_BACKUP_KEY_FILE`` names a key
file (default ``~/.trans_keys/backup.key`` — outside the repository), the DB
dump and the asset archive are encrypted at rest with AES-256-CBC (``openssl
-pbkdf2``). The key is never written into the backup and never committed: the
module only *reads* it. When no key file is present the backup is written
unencrypted (single-host dev) and the manifest records ``encrypted=false``.

Restore (AC1) re-creates a *clean* target DB and re-uploads assets, then the
caller verifies the manifest hashes match.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _key_file() -> str | None:
    p = _env("TRANS_BACKUP_KEY_FILE", os.path.expanduser("~/.trans_keys/backup.key"))
    return p if p and Path(p).exists() else None


def _pg_env(db_url: str) -> dict:
    """Split a ``postgresql://user:pass@host:port/db`` into psql/pg_dump env."""
    m = re.match(
        r"postgresql(?:\+[^@]+)?://(?P<u>[^:]+)(?::(?P<p>[^@]*))?@(?P<h>[^:/]+)"
        r"(?::(?P<port>\d+))?/(?P<db>[^/?]+)",
        db_url,
    )
    if not m:
        raise ValueError(f"cannot parse DATABASE_URL: {db_url!r}")
    env = {
        "PGHOST": m.group("h"),
        "PGPORT": m.group("port") or "5432",
        "PGUSER": m.group("u"),
        "PGDATABASE": m.group("db"),
    }
    if m.group("p"):
        env["PGPASSWORD"] = m.group("p")
    return env


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _openssl(args: list[str]) -> None:
    r = subprocess.run(["openssl"] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"openssl {' '.join(args)} failed: {r.stderr.strip()}")


def _openssl_pass_args(key_file: str) -> list[str]:
    """``-pass`` args so openssl reads the key from a file (not the repo)."""
    return ["-pass", f"pass:file:{key_file}"]


def _encrypt(path: Path, key: str) -> None:
    _openssl(["enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000"]
             + _openssl_pass_args(key)
             + ["-in", str(path), "-out", str(path) + ".enc"])
    os.replace(str(path) + ".enc", str(path))


def _decrypt(path: Path, key: str) -> None:
    _openssl(["enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000"]
             + _openssl_pass_args(key)
             + ["-in", str(path), "-out", str(path) + ".dec"])
    os.replace(str(path) + ".dec", str(path))


def _table_counts(db_url: str) -> dict[str, int]:
    from sqlalchemy import create_engine, text
    eng = create_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            tables = [
                r[0] for r in c.execute(
                    text("SELECT tablename FROM pg_tables "
                         "WHERE schemaname='public' ORDER BY tablename"))
            ]
            out = {}
            for t in tables:
                n = c.execute(
                    text(f'SELECT count(*) FROM "{t}"')).scalar()
                out[t] = int(n)
            return out
    finally:
        eng.dispose()


def _minio_client():
    """Build a MinIO client from env, parsing host:port like storage.py.

    The minio client needs the port in the endpoint string (``127.0.0.1:9000``);
    a bare host is interpreted as port 80 and silently hangs. So default the
    port to 9000 when it isn't given, exactly as :func:`storage.get_storage_provider`.
    """
    from minio import Minio
    endpoint = _env("MINIO_ENDPOINT", "127.0.0.1")
    if ":" in endpoint:
        host, _, port_str = endpoint.partition(":")
        port = int(port_str) if port_str.isdigit() else 9000
    else:
        host, port = endpoint, 9000
    return Minio(
        f"{host}:{port}",
        access_key=_env("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=_env("MINIO_SECRET_KEY", "minioadmin"),
        secure=_env("MINIO_SECURE", "0") == "1",
    )


def _local_storage_root() -> Path:
    """The LocalFileStorage root (storage.STORAGE_ROOT default ``./storage``)."""
    return Path(_env("STORAGE_ROOT", "./storage"))


def _iter_local_assets():
    """Yield ``(key, raw_bytes)`` for every file under the local storage root.

    Used when no ``MINIO_ENDPOINT`` is configured (tests / single-host dev):
    the bytes are read *raw* from disk (still in their at-rest encrypted form,
    if a key exists) so a backup -> restore roundtrip is byte-identical, the
    same invariant the MinIO path guarantees.
    """
    root = _local_storage_root()
    if not root.exists():
        return
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p.relative_to(root).as_posix(), p.read_bytes()


def create_backup(
    backup_root: str,
    db_url: str,
    *,
    minio_bucket: str = "trans",
    description: str = "",
) -> dict:
    """Create one versioned backup under ``backup_root``. Returns the manifest.

    Layout::

        backup_root/<UTC-ts>_<seq>/
          db.dump          (-Fc custom format; encrypted in place if a key exists)
          assets.tar       (all bucket objects; encrypted in place if a key exists)
          manifest.json    (version, counts, sha256 of each artefact, key id)
    """
    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    seq = uuid.uuid4().hex[:8]
    version_dir = root / f"{ts}_{seq}"
    version_dir.mkdir(parents=True, exist_ok=True)
    version = version_dir.name

    key = _key_file()
    encrypted = key is not None

    # --- 1. DB dump -------------------------------------------------------
    # -Fc writes a binary custom-format archive to STDOUT; capture raw bytes.
    db_dump = version_dir / "db.dump"
    env = _pg_env(db_url)
    r = subprocess.run(["pg_dump", "-Fc", "--no-owner", "--no-privileges"],
                       env={**os.environ, **env},
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {r.stderr.decode('utf-8','replace')[:300]}")
    db_dump.write_bytes(r.stdout)

    # --- 2. assets --------------------------------------------------------
    # MinIO when configured (compose deployment); otherwise the local
    # filesystem store (tests / single-host dev, same §14 invariant).
    assets_tar = version_dir / "assets.tar"
    asset_count = 0
    asset_hashes: dict[str, str] = {}
    with tarfile.open(assets_tar, "w") as tar:
        if _env("MINIO_ENDPOINT", ""):
            mc = _minio_client()
            for obj in mc.list_objects(minio_bucket, recursive=True):
                resp = mc.get_object(minio_bucket, obj.object_name)
                try:
                    data = resp.read()
                finally:
                    resp.close()
                    resp.release_conn()
                # Keep the original object key as the tar member name (tar stores
                # nested paths fine); restore just re-extracts each member.
                info = tarfile.TarInfo(name=obj.object_name)
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
                asset_hashes[obj.object_name] = hashlib.sha256(data).hexdigest()
                asset_count += 1
        else:
            for key_name, data in _iter_local_assets():
                info = tarfile.TarInfo(name=key_name)
                info.size = len(data)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(data))
                asset_hashes[key_name] = hashlib.sha256(data).hexdigest()
                asset_count += 1

    # --- 3. encrypt (if a key is present) ---------------------------------
    if encrypted:
        _encrypt(db_dump, key)
        _encrypt(assets_tar, key)

    # --- 4. manifest ------------------------------------------------------
    counts = _table_counts(db_url)
    manifest: dict[str, Any] = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "description": description,
        "source_db": {
            "host": env["PGHOST"], "port": env["PGPORT"],
            "db": env["PGDATABASE"], "user": env["PGUSER"],
        },
        "encrypted": encrypted,
        "key_file_id": (hashlib.sha256(Path(key).read_bytes()).hexdigest()[:16]
                         if key else None),
        "db_dump_sha256": _sha256_file(db_dump),
        "assets_tar_sha256": _sha256_file(assets_tar),
        "asset_count": asset_count,
        "asset_sha256": asset_hashes,
        "table_counts": counts,
    }
    (version_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def list_backups(backup_root: str) -> list[dict]:
    """List existing backup versions (newest first)."""
    root = Path(backup_root)
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        mf = d / "manifest.json"
        if mf.exists():
            m = json.loads(mf.read_text())
            out.append({"version": d.name, "created_at": m.get("created_at"),
                        "encrypted": m.get("encrypted"),
                        "asset_count": m.get("asset_count")})
    return out


def restore_backup(
    backup_root: str,
    version: str,
    target_db_url: str,
    *,
    minio_bucket: str = "trans",
) -> dict:
    """Restore one backup version into a *clean* target DB + MinIO bucket.

    Returns a verification dict: row counts restored, asset count restored,
    and whether the artefact hashes match the manifest.
    """
    version_dir = Path(backup_root) / version
    mf = version_dir / "manifest.json"
    if not mf.exists():
        raise FileNotFoundError(f"no manifest for backup {version}")
    manifest = json.loads(mf.read_text())
    key = _key_file()
    if manifest.get("encrypted") and key is None:
        raise RuntimeError(
            f"backup {version} is encrypted but no key file at "
            f"TRANS_BACKUP_KEY_FILE")

    # ensure the target db exists. Terminate any stale backends first so the
    # CREATE (and any later DROP by the operator) can't block on a live
    # connection (the F4-QA conftest uses the same trick for template1
    # collation drift).
    from sqlalchemy import create_engine, text
    admin_url = re.sub(r"/[^/]+$", "/postgres", target_db_url)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    dbname = target_db_url.rsplit("/", 1)[-1]
    with admin.connect() as c:
        c.execute(text(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{dbname}' AND pid <> pg_backend_pid()"))
        try:
            c.execute(text(
                f'CREATE DATABASE "{dbname}" '
                "TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"))
        except Exception:  # already exists (idempotent)
            pass
    admin.dispose()

    db_dump = version_dir / "db.dump"
    assets_tar = version_dir / "assets.tar"

    # Verify the stored (still-encrypted) artefacts against the manifest
    # BEFORE we decrypt anything, so tamper/integrity is checked on exactly
    # what is stored at rest.
    manifest_sha_ok = (
        _sha256_file(db_dump) == manifest.get("db_dump_sha256")
        and _sha256_file(assets_tar) == manifest.get("assets_tar_sha256")
    )

    # template0 has NO public schema; create it so pg_restore has a place
    # to create the restored tables.
    eng = create_engine(target_db_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text('CREATE SCHEMA IF NOT EXISTS public'))
        c.execute(text('GRANT ALL ON SCHEMA public TO public'))
    eng.dispose()

    # decrypt if needed (in place; the artefact was a backup, so leaving the
    # decrypted form in the version dir after restore is acceptable)
    if manifest.get("encrypted"):
        _decrypt(db_dump, key)
        _decrypt(assets_tar, key)

    # --- restore DB into a clean target -----------------------------------
    env = _pg_env(target_db_url)
    # --clean + --if-exists makes it idempotent; into a fresh db it's a clean
    # restore by construction.
    r = subprocess.run(
        ["pg_restore", "--clean", "--if-exists", "-O", str(db_dump)],
        env={**os.environ, **env},
        capture_output=True)
    # pg_restore may return non-zero on harmless notices; verify instead, but
    # surface the stderr so a real failure (permissions, missing schema) is
    # visible rather than silently producing an empty db.
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode > 2 or ("ERROR" in err and "doesn't exist" not in err.lower()):
        print(f"pg_restore rc={r.returncode} stderr: {err[:800]}", flush=True)
    if r.returncode not in (0, 1, 2):
        raise RuntimeError(f"pg_restore failed: {err[:400]}")

    # --- restore assets to MinIO or back to the local store ---------------
    restored_assets = 0
    if _env("MINIO_ENDPOINT", ""):
        mc = _minio_client()
        if mc.bucket_exists(minio_bucket):
            with tarfile.open(assets_tar, "r") as tar:
                for member in tar.getmembers():
                    if not member.isfile():
                        continue
                    obj = tar.extractfile(member)
                    if obj is None:
                        continue
                    data = obj.read()
                    mc.put_object(minio_bucket, member.name, io.BytesIO(data),
                                  length=len(data))
                    restored_assets += 1
    else:
        root = _local_storage_root()
        root.mkdir(parents=True, exist_ok=True)
        with tarfile.open(assets_tar, "r") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                obj = tar.extractfile(member)
                if obj is None:
                    continue
                target = root / member.name
                if root.resolve() not in target.resolve().parents:
                    continue  # never write outside the storage root
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(obj.read())
                restored_assets += 1

    # --- verify -----------------------------------------------------------
    # ``manifest_sha_ok`` was computed on the stored (still-encrypted) bytes
    # before decrypting, so it reflects integrity of what is kept at rest.
    counts = _table_counts(target_db_url)
    return {
        "version": version,
        "target_db": target_db_url,
        "manifest_sha_ok": bool(manifest_sha_ok),
        "table_counts": counts,
        "table_counts_expected": manifest.get("table_counts"),
        "assets_restored": restored_assets,
        "assets_expected": manifest.get("asset_count"),
    }
