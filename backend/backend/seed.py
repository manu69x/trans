"""Seed a bootstrap admin account from environment variables (PRD §2.1, §13.1).

The platform ships with no users. When ``SEED_ADMIN_EMAIL`` and
``SEED_ADMIN_PASSWORD`` are both set they are turned into an ``admin`` account
on first run (idempotent: an existing user with that email is left untouched
unless the password hash differs, in which case it is re-hashed). This mirrors
the "seed di un utente admin da env var" requirement and keeps the secret out
of the repository.
"""

from __future__ import annotations

import os
import sys
import uuid

# Make ``backend`` importable when run as a module from the repo root or backend dir.
for _p in (os.getcwd(), os.path.join(os.getcwd(), "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.audit import log_event  # noqa: E402
from backend.db import SessionLocal  # noqa: E402
from backend.models import User  # noqa: E402
from backend.rbac import ROLE_PERMISSIONS  # noqa: E402
from backend.security import hash_password, verify_password  # noqa: E402


def seed_admin_from_env() -> "User | None":
    """Create/refresh the admin user from ``SEED_ADMIN_*`` env vars.

    Returns the (existing or newly created) admin :class:`User`, or ``None``
    when seeding was not requested (both env vars must be set).
    """
    email = os.getenv("SEED_ADMIN_EMAIL", "").strip()
    password = os.getenv("SEED_ADMIN_PASSWORD", "")
    if not email or not password:
        return None

    with SessionLocal() as db:
        user = db.query(User).filter(User.email.ilike(email)).first()
        need_hash = user is None or not verify_password(password, user.password_hash)
        if user is None:
            user = User(id=str(uuid.uuid4()))
        user.email = email
        user.role = "admin"
        user.is_active = True
        if need_hash:
            user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
        db.refresh(user)
        log_event(
            db=db,
            user_id=user.id,
            action="seed_admin",
            entity="auth",
            after={"email": user.email},
        )
    return user


def main() -> int:  # console-script style entrypoint
    user = seed_admin_from_env()
    if user is None:
        print("SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD not set; skipped.")
        return 0
    print(f"Admin user seeded/updated: {user.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
