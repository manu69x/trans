"""LLM Gateway adapter (PRD §8, ADR-001) — the ONLY LLM inference path.

Owns every network detail so the rest of the platform never imports an HTTP
client to reach a model:

* base URL / key from :mod:`backend.config` (``LLM_GATEWAY_BASE_URL`` /
  ``LLM_GATEWAY_API_KEY``); the default is the ADR-001 measured proxy on the
  loopback — inside the §13.1 local-only network;
* ``list_models`` reads ``GET /v1/models`` (§8.1: never hardcode names);
  llama-swap models carry ``status.value in {loaded, unloaded}`` and context
  hints inside ``name`` (ADR-001 §3.2/§3.3), surfaced as ``context_window``;
* :func:`chat_json` performs the JSON-constrained completion
  (``response_format: {"type": "json_object"}``, ADR-001 §3.3: supported and
  validated) plus ``reasoning_effort: "none"`` (§8.4: visible reasoning must
  not leak into the output nor become unpredictable cost);
* rate limiting (``LLM_MAX_RPS``) and bounded retry with backoff on 429/5xx
  (§8.4: safe pause, idempotency — the caller keys retries by block hash);
* the §13.1 gate: :func:`assert_local_url` runs on the base URL before the
  first call — a non-local endpoint raises, blocking any manuscript payload
  from leaving the machine (acceptance criterion 3).

Errors: :class:`GatewayUnavailable` (connection/HTTP failure after retries),
:class:`GatewayInvalidJSON` (2xx whose content is not valid JSON). The
caller decides policy (skip the block, mark the run failed); the adapter
never raises silently and never logs prompt payloads (§13.2: log JSON
sanitizzati — only hashes, sizes and statuses).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from typing import Any

import httpx

from .config import (
    LLM_MAX_RETRIES,
    LLM_MAX_RPS,
    LLM_GATEWAY_API_KEY,
    LLM_GATEWAY_BASE_URL,
    LLM_GATEWAY_MAX_OUTPUT_TOKENS,
    assert_local_url,
)


class GatewayUnavailable(RuntimeError):
    """Gateway not reachable or persistent HTTP failure (after retries)."""


class GatewayInvalidJSON(RuntimeError):
    """HTTP 2xx but the message content is not valid JSON."""


_CTX_HINT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(K|M)\b", re.IGNORECASE)


def _context_from_name(name: str) -> int | None:
    """Derive the context window from llama-swap name hints (ADR-001 §3.2).

    The proxy does not expose ``context_length``; names carry hints such as
    ``256K`` or ``1M`` (measured range 2K … 1_000_000). ``16K`` → 16 384,
    ``1M`` → 1 000 000 (llama-swap writes decimal mega-hints).
    """
    m = _CTX_HINT.search(name or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    if m.group(2).upper() == "M":
        return int(value * 1_000_000)
    return int(value * 1024)


class RateLimiter:
    """Simple thread-safe pacesetter: at most ``max_rps`` starts per second."""

    def __init__(self, max_rps: float) -> None:
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self) -> float:
        """Block until the caller may proceed; returns the seconds waited."""
        with self._lock:
            now = time.monotonic()
            slot = max(self._next_slot, now)
            self._next_slot = slot + self._min_interval
        wait = slot - now
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)


_shared_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def shared_rate_limiter() -> RateLimiter:
    """Process-wide limiter shared by all Gateway calls (§8.4)."""
    global _shared_limiter
    with _limiter_lock:
        if _shared_limiter is None:
            _shared_limiter = RateLimiter(LLM_MAX_RPS)
        return _shared_limiter


def reset_rate_limiter() -> None:
    """Test helper: drop the shared limiter (config may have changed)."""
    global _shared_limiter
    with _limiter_lock:
        _shared_limiter = None


def sha256_hex(data: str | bytes) -> str:
    """Hash helper — prompts land in the DB only as hashes (§8.5)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _parse_model_json(content: str):
    """Best-effort parse of a json_object-constrained completion (§9.5).

    Small local models keep wrapping the constrained payload in a
    ```json fence (or a stray "json" word) and may emit invalid escapes
    such as ``\\'`` inside string values. The wire contract is still the
    proxy's ``response_format: json_object``; this only tolerates the
    transport artefacts so a fenced-but-valid payload is not scored as a
    schema failure. Raises :class:`json.JSONDecodeError` when no valid
    JSON value can be recovered.
    """
    text = content.strip()
    # strip one fenced block: ```json ... ``` / ``` ... ```
    fence = re.match(
        r"^```[a-zA-Z0-9]*\s*\n?(.*?)\n?\s*```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    elif text.lower().startswith("json"):
        text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # i piccoli modelli inseriscono a volte caratteri di controllo grezzi
    # (newline reali dentro le stringhe): strict=False li accetta come
    # spazio bianco invece di rigettare tutto il blocco (fix 2026-09-21).
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        pass
    # invalid escape sequences (e.g. \' inside strings): neutralise the
    # backslash before a quote, then retry once.
    repaired = re.sub(r"\\(['\"])", r"\1", text)
    return json.loads(repaired, strict=False)


class GatewayClient:
    """OpenAI-compatible client for the local LLM Gateway (ADR-001 §3)."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 600.0,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._base_url = assert_local_url(base_url or LLM_GATEWAY_BASE_URL)
        self._api_key = api_key if api_key is not None else LLM_GATEWAY_API_KEY
        self._timeout = timeout
        self._limiter = limiter or shared_rate_limiter()

    # --- §8.1 model discovery -------------------------------------------
    def list_models(self) -> list[dict[str, Any]]:
        """``GET /v1/models`` → capability-matrix rows (§8.1, no hardcoding).

        Rows: ``id``, ``display_name`` (llama-swap ``name``), ``provider``
        (``owned_by``), ``status`` (``loaded``/``unloaded`` — readiness per
        ADR-001 §3.2), ``supports_json_schema=True`` (ADR-001 §3.3: measured),
        ``context_window`` (hint-derived, may be ``None``).
        """
        data = self._request("GET", "/models")
        rows: list[dict[str, Any]] = []
        for item in data.get("data", []):
            model_id = str(item.get("id") or "")
            name = str(item.get("name") or model_id)
            rows.append({
                "id": model_id,
                "display_name": name,
                "provider": item.get("owned_by") or "llama-swap",
                "status": (item.get("status") or {}).get("value") or "unknown",
                "supports_json_schema": True,  # ADR-001 §3.3 (measured)
                "supports_streaming": True,    # ADR-001 §3.3 (measured)
                "context_window": _context_from_name(name),
            })
        return rows

    def model_ready(self, model_id: str) -> bool:
        """True when *model_id* is listed (llama-swap loads on demand)."""
        return any(m["id"] == model_id for m in self.list_models())

    # --- §8.3 LlmRunRequest ----------------------------------------------
    def chat_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        seed: int | None = None,
    ) -> dict[str, Any]:
        """Structured completion: JSON-constrained, reasoning off (§8.3/§8.4).

        Returns the parsed JSON object. Retries transient failures
        (429/5xx/timeouts) with capped exponential backoff; a 2xx response
        whose content is not JSON raises :class:`GatewayInvalidJSON` — the
        caller records the run as ``schema_error`` and continues (§9.5).
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": max(0.0, min(temperature, 0.2)),
            "max_tokens": min(
                max_tokens if max_tokens else LLM_GATEWAY_MAX_OUTPUT_TOKENS,
                LLM_GATEWAY_MAX_OUTPUT_TOKENS,
            ),
            "response_format": {"type": "json_object"},  # ADR-001 §3.3
            "reasoning_effort": "none",  # §8.4: no reasoning in the output
        }
        if seed is not None:
            payload["seed"] = seed  # ADR-001 §3.3: accepted

        attempt = 0
        while True:
            self._limiter.acquire()
            attempt += 1
            try:
                data = self._request("POST", "/chat/completions", json_body=payload)
            except GatewayUnavailable:
                if attempt > LLM_MAX_RETRIES:
                    raise
                time.sleep(min(2 ** attempt, 8))  # bounded backoff (§8.4)
                continue
            content = (data.get("choices") or [{}])[0].get(
                "message", {}).get("content")
            if content is None or not content.strip():
                # Empty content happens under load (llama-server --parallel 1
                # + json grammar): retryable rather than a hard schema failure.
                if attempt > LLM_MAX_RETRIES:
                    raise GatewayInvalidJSON(
                        f"empty completion content after {attempt} attempts")
                time.sleep(min(2 ** attempt, 8))
                continue
            try:
                return _parse_model_json(content)
            except json.JSONDecodeError as exc:
                if attempt > LLM_MAX_RETRIES:
                    raise GatewayInvalidJSON(str(exc)) from exc
                time.sleep(min(2 ** attempt, 8))
                continue

    # --- transport ---------------------------------------------------------
    def _request(
        self, method: str, path: str, json_body: dict | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            response = httpx.request(
                method,
                self._base_url + path,
                json=json_body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GatewayUnavailable(str(exc)) from exc
        if response.status_code == 401:
            raise GatewayUnavailable(
                "unauthorized: check LLM_GATEWAY_API_KEY (ADR-001 §3.1)")
        if response.status_code in (429,) or response.status_code >= 500:
            raise GatewayUnavailable(f"HTTP {response.status_code} from Gateway")
        if response.status_code >= 400:
            raise GatewayInvalidJSON(
                f"HTTP {response.status_code} from Gateway {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise GatewayInvalidJSON(str(exc)) from exc
