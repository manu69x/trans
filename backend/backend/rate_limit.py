"""Per-IP token-bucket rate limiting for sensitive endpoints (PRD §13/§14).

A dependency-free in-process limiter (the deployment is single-host,
local-only): each ``(bucket, client-ip)`` pair owns a token bucket with a
burst capacity and a refill rate. :func:`enforce` raises
:class:`RateLimitExceeded` when the caller exceeds the budget, which the
FastAPI dependency translates into an HTTP 429 with ``Retry-After``.

Buckets (env-overridable):

* ``auth``      -- login/refresh: 10 burst, 2/min refill (brute-force guard)
* ``upload``    -- document upload: 10 burst, 5/min refill
* ``translate`` -- LLM translation runs: 30 burst, 10/min refill (§8.4: il
   frontend invia un POST da ~20 segmenti per blocco; il POST accoda solo il
   job. 5/1-min affogava il flusso normale: capitoli >100 segmenti o un
   semplice retry ricevevano 429. La protezione reale resta il token budget
   §5.4 per blocco e la coda, non il rate.)
* ``export``    -- exports/downloads: 20 burst, 10/min refill
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field


class RateLimitExceeded(Exception):
    """Raised by :func:`enforce` when a bucket has no tokens left."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(1, int(round(retry_after)))
        super().__init__(f"rate limit exceeded, retry in {self.retry_after}s")


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """Thread-safe token-bucket registry keyed by ``(bucket, identity)``."""

    limits: dict[str, tuple[float, float]] = field(default_factory=dict)
    buckets: dict[tuple[str, str], _Bucket] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, bucket: str, identity: str, *, now: float | None = None,
              cost: float = 1.0) -> tuple[bool, float]:
        """Try to take *cost* tokens; return ``(allowed, retry_after_s)``."""
        if bucket not in self.limits:
            raise KeyError(f"unknown rate-limit bucket: {bucket!r}")
        capacity, refill = self.limits[bucket]
        t = time.monotonic() if now is None else now
        key = (bucket, identity)
        with self.lock:
            b = self.buckets.get(key)
            if b is None:
                b = _Bucket(capacity, refill, float(capacity), t)
                self.buckets[key] = b
            else:
                elapsed = max(0.0, t - b.updated)
                b.tokens = min(capacity, b.tokens + elapsed * refill)
                b.updated = t
            if b.tokens >= cost:
                b.tokens -= cost
                return True, 0.0
            deficit = cost - b.tokens
            return False, deficit / refill if refill > 0 else float("inf")

    def reset(self) -> None:
        with self.lock:
            self.buckets.clear()


def _f(name: str, default: float) -> float:
    try:
        v = float(os.getenv(name, ""))
        return v if v > 0 else default
    except ValueError:
        return default


#: Process-wide limiter with the §13 default buckets.
limiter = RateLimiter(limits={
    "auth": (_f("TRANS_RATE_AUTH_BURST", 10.0), _f("TRANS_RATE_AUTH_RPM", 2.0) / 60.0),
    "upload": (_f("TRANS_RATE_UPLOAD_BURST", 10.0), _f("TRANS_RATE_UPLOAD_RPM", 5.0) / 60.0),
    "translate": (_f("TRANS_RATE_TRANSLATE_BURST", 30.0), _f("TRANS_RATE_TRANSLATE_RPM", 10.0) / 60.0),
    "export": (_f("TRANS_RATE_EXPORT_BURST", 20.0), _f("TRANS_RATE_EXPORT_RPM", 10.0) / 60.0),
    "default": (_f("TRANS_RATE_DEFAULT_BURST", 120.0), _f("TRANS_RATE_DEFAULT_RPM", 600.0) / 60.0),
    # project delete (§12.1): destructive but rare — small burst, slow refill
    "project_delete": (_f("TRANS_RATE_DELETE_BURST", 5.0), _f("TRANS_RATE_DELETE_RPM", 2.0) / 60.0),
})
