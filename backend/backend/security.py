"""Password hashing with Argon2id (PRD §13.1, §2).

Argon2id is the memory-hard KDF recommended for password hashing. Hashes
are stored in the ``password_hash`` column of ``users`` and verified with
:func:`verify_password`. A per-user salt is embedded in the returned hash
(standard ``argon2`` encoding), so no separate salt column is needed.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError as Argon2VerificationError

# The default PasswordHasher uses the recommended parameters (Argon2id) and a
# random per-call salt. Construction is intentionally module-level so all
# callers share one configuration.
_ph = PasswordHasher()


def hash_password(plain: str) -> str:
    """Return an Argon2id hash of *plain* (safe to store in the DB)."""
    return _ph.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    """Verify *plain against *stored_hash.

    Returns False on a failed verification or on a malformed/unknown hash
    rather than raising, so callers can treat auth failures uniformly.
    """
    try:
        return _ph.verify(stored_hash, plain)
    except (Argon2VerificationError, ValueError):
        return False
