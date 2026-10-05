"""Encryption helpers for stored assets and backups (PRD §13.1).

Documents at rest must be encrypted on local disk. :mod:`backend.backup`
already encrypts *backups* with an ``openssl enc -aes-256-cbc -pbkdf2`` key
file outside the repository; this module gives the same envelope to the
*live* object store so ``LocalFileStorage`` payloads are never written
plaintext.

Envelope design (deterministic, streaming-friendly):

    ``TENC1<12-byte nonce><AES-256-GCM ciphertext+tag>``

Key source: the file named by ``TRANS_STORAGE_KEY_FILE`` (default
``~/.trans_keys/storage.key``, outside the repo — §13.1 "chiavi gestite
fuori dal repository"). If the key file is absent, :func:`get_key` returns
``None`` and :func:`maybe_encrypt` / :func:`maybe_decrypt` act as the
identity (single-host dev keeps working unencrypted).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

_MAGIC = b"TENC1"
_NONCE_LEN = 12


def key_file_path() -> str:
    return os.getenv(
        "TRANS_STORAGE_KEY_FILE",
        os.path.expanduser("~/.trans_keys/storage.key"),
    )


def get_key() -> bytes | None:
    """The 32-byte storage key from ``TRANS_STORAGE_KEY_FILE`` (or None)."""
    p = key_file_path()
    if not p or not Path(p).exists():
        return None
    raw = Path(p).read_bytes().strip()
    if not raw:
        return None
    # Accept raw 32 bytes or any passphrase (keyed by SHA-256 to 32 bytes).
    return raw if len(raw) == 32 else hashlib.sha256(raw).digest()


def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM envelope: magic + nonce + ciphertext(+tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(_NONCE_LEN)
    return _MAGIC + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Reverse :func:`encrypt`; raises on tampering (GCM tag check)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < _MIN_LEN:
        raise ValueError("encrypted blob too short")
    if not blob.startswith(_MAGIC):
        raise ValueError("bad magic: not a TENC1 payload")
    nonce = blob[len(_MAGIC):len(_MAGIC) + _NONCE_LEN]
    ct = blob[len(_MAGIC) + _NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, None)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(_MAGIC)


def maybe_encrypt(data: bytes, key: bytes | None) -> bytes:
    return encrypt(data, key) if key else data


def maybe_decrypt(blob: bytes, key: bytes | None) -> bytes:
    if key is None:
        return blob
    return decrypt(blob, key) if is_encrypted(blob) else blob


def generate_key_file(path: str | None = None) -> str:
    """Write a fresh 32-byte key to *path* (chmod 0600) and return the path.

    Used by ops/bootstrap: the key lives outside the repository.
    """
    p = Path(path or key_file_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(secrets.token_bytes(32))
    p.chmod(0o600)
    return str(p)


_MAGIC_LEN_TOTAL = len(_MAGIC) + _NONCE_LEN + 16
_MIN_LEN = _MAGIC_LEN_TOTAL
