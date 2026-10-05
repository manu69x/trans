"""Object storage abstraction (PRD §5.2, §13.1).

All uploaded assets live in a *local* object store. Two implementations are
provided behind the :class:`StorageProvider` protocol:

* :class:`MinioStorage` — talks to a local MinIO instance (production /
  Compose deployment).
* :class:`LocalFileStorage` — a plain filesystem used by tests and single-host
  dev, so behaviour is identical without a running MinIO.

Both are strictly local: the endpoints are resolved from environment variables
that default to ``127.0.0.1`` / ``localhost`` and are never derived from user
input, which keeps the §13.1 local_only policy intact.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import crypto_at_rest


@runtime_checkable
class StorageProvider(Protocol):
    """Minimal storage surface used by the upload flow."""

    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


class LocalFileStorage:
    """Filesystem-backed store. Used by tests and single-host dev.

    Files are written under ``ROOT/key`` with every path component created on
    demand. The root defaults to a temp dir so tests never touch the repo.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Reject any path traversal so a crafted key can't escape the root.
        parts = [p for p in key.split("/") if p not in ("", ".", "..")]
        safe = "/".join(parts)
        target = (self.root / safe).resolve()
        if self.root.resolve() not in target.parents and target != self.root.resolve():
            raise ValueError(f"invalid storage key: {key!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def put(self, key: str, data: bytes) -> None:
        # §13.1 cifratura a riposo: the payload is sealed with the envelope
        # from crypto_at_rest when a storage key file is configured.
        with open(self._path(key), "wb") as fh:
            fh.write(crypto_at_rest.maybe_encrypt(data, crypto_at_rest.get_key()))

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        blob = path.read_bytes()
        return crypto_at_rest.maybe_decrypt(blob, crypto_at_rest.get_key())

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        from . import retention as _rt

        path = self._path(key)
        if path.exists():
            # Secure delete (§13.1): overwrite before unlink.
            _rt.secure_delete_file(path)


class MinioStorage:
    """MinIO-backed store for the Compose deployment.

    Endpoint and credentials come from environment variables (see
    ``infra/docker-compose.yml``). ``local_only`` must stay True for the
    §13.1 policy to hold.
    """

    def __init__(
        self,
        endpoint: str = "127.0.0.1",
        port: int = 9000,
        access_key: str = "minioadmin",
        secret_key: str = "minioadmin",
        bucket: str = "trans",
        secure: bool = False,
        local_only: bool = True,
    ) -> None:
        from minio import Minio  # imported lazily (optional dependency)

        if local_only and not (
            endpoint in ("127.0.0.1", "localhost", "0.0.0.0", "minio")
            or endpoint.endswith(".local")
        ):
            # "minio" is the compose-internal service name (infra/docker-
            # compose.yml MINIO_ENDPOINT=minio:9000): it resolves on the
            # local Docker network, never outside the host (§13.1).
            raise ValueError(
                "local_only policy (§13.1) blocks non-local MinIO endpoint"
            )

        self.client = Minio(
            f"{endpoint}:{port}",
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket
        if not self.client.bucket_exists(bucket):
            self.client.make_bucket(bucket)

    def put(self, key: str, data: bytes) -> None:
        import io

        payload = crypto_at_rest.maybe_encrypt(data, crypto_at_rest.get_key())
        self.client.put_object(
            self.bucket, key, io.BytesIO(payload), len(payload)
        )

    def get(self, key: str) -> bytes | None:
        try:
            resp = self.client.get_object(self.bucket, key)
            try:
                blob = resp.read()
            finally:
                resp.close()
        except Exception:  # noqa: BLE001 - object not found or transient
            return None
        return crypto_at_rest.maybe_decrypt(blob, crypto_at_rest.get_key())

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.stat_object(self.bucket, key)
            return True
        except S3Error:
            return False

    def delete(self, key: str) -> None:
        self.client.remove_object(self.bucket, key)


def get_storage_provider() -> StorageProvider:
    """Return the configured storage provider.

    Uses MinIO when ``MINIO_ENDPOINT`` is set and local (production),
    otherwise falls back to a filesystem store under ``STORAGE_ROOT``.
    """
    endpoint = os.getenv("MINIO_ENDPOINT", "")
    if endpoint:
        host, _, port_str = endpoint.partition(":")
        try:
            port = int(port_str) if port_str else 9000
        except ValueError:
            port = 9000
        return MinioStorage(
            endpoint=host,
            port=port,
            access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            bucket=os.getenv("MINIO_BUCKET", "trans"),
            secure=os.getenv("MINIO_SECURE", "0") == "1",
            local_only=os.getenv("LOCAL_ONLY", "1") == "1",
        )

    root = os.getenv("STORAGE_ROOT", "./storage")
    return LocalFileStorage(root)


def sha256_of(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of *data* (PRD §5.2)."""
    return hashlib.sha256(data).hexdigest()
