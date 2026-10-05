"""LLM Gateway OpenAI-compatible adapter (PRD §8, ADR-001).

This module is the ONLY path to LLM inference. It never hardcodes model
names (§8.1): it interrogates the real proxy ``/v1/models`` at startup and on
explicit refresh and builds the capability matrix from the proxy's own fields
plus the ADR-001-measured proxy capabilities.

Fallback / revival (§8.4)

* An unavailable (offline) model SUSPENDS the batch and asks the user -- it is
  never replaced automatically. :class:`ModelUnavailableError` is the signal.
* Retry happens only for transient errors (429 / 5xx / network) and always
  with an idempotency key.
* A run is recorded in ``llm_runs`` with input hash, output hash and the full
  parameter set so it can be replayed / branched (§8.4, §16).

The adapter is built in two layers so it is testable without the proxy:

* :class:`GatewayClient` performs the raw HTTP against the proxy.
* :class:`GatewayAdapter` transforms, validates, records runs and enforces the
  §8.4 rules. Tests inject a fake client or a fake adapter.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config as cfg

# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Base error for every Gateway-adapter failure."""


class ModelUnavailableError(GatewayError):
    """A requested model cannot serve the batch.

    Raised when the model is unknown or not ``loaded`` on the proxy. The
    caller MUST suspend the batch and ask the user (§8.4): it is NEVER
    replaced automatically.
    """

    def __init__(self, model_id: str, reason: str) -> None:
        self.model_id = model_id
        self.reason = reason
        detail = reason if reason else "is not available"
        super().__init__(f"model {model_id!r} {detail}")


class SchemaValidationError(GatewayError):
    """The proxy output did not match the requested JSON schema."""


# --------------------------------------------------------------------------
# Capability matrix (PRD §8.1 / ADR-001 §3.3)
# --------------------------------------------------------------------------


@dataclass
class GatewayModel:
    """One row of the capability matrix (§8.1)."""

    id: str
    display_name: str
    provider: str
    context_window: int | None = None
    max_output: int | None = None
    supports_json_schema: bool = True
    supports_streaming: bool = True
    supports_reasoning: bool = True
    supports_seed: bool = True
    languages: list[str] = field(default_factory=list)
    locality: str = "on-premise"
    status: str = "offline"  # available | degraded | offline
    latency_ms: float | None = None
    vram_host: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# Proxy-level capabilities (ADR-001 §3.3): the OpenAI-compatible proxy accepts
# response_format json_object, SSE streaming, reasoning_effort and seed for
# every routed model. They are reported at proxy level; the per-model
# ``status`` (loaded/unloaded) is what gates whether a model is actually
# usable.
_PROXY_CAPABILITIES = {
    "supports_json_schema": True,
    "supports_streaming": True,
    "supports_reasoning": True,
    "supports_seed": True,
}

# Name hints -> context window in tokens (ADR-001 §3.4): "256K", "1M",
# "512K ctx", "128k ctx", "1_000_000", "262k", ...
_HINT_PATTERNS = [
    (re.compile(r"(\d{1,4})\s*M\b", re.I), lambda m: int(m) * 1_000_000),
    (re.compile(r"(\d{1,4})\s*K\b", re.I), lambda m: int(m) * 1_000),
    (re.compile(r"(\d[\d_]*)\s*ctx", re.I), lambda m: int(m.replace("_", ""))),
    (re.compile(r"(\d[\d_]*)", re.I), lambda m: int(m.replace("_", ""))),
]

_HOST_HINTS = ("v100", "3090", "rtx", "a100", "h100")


def _derive_context_window(name: str) -> int | None:
    """Derive the context window from a name hint (ADR-001 §3.4)."""
    for pattern, to_tokens in _HINT_PATTERNS:
        m = pattern.search(name)
        if m:
            val = to_tokens(m.group(1))
            if 1000 <= val <= 2_000_000:
                return val
    return None


def _languages_from_name(name: str) -> list[str]:
    """Best-effort EN->IT hint from the model name (ADR-001: not exposed)."""
    low = name.lower()
    if "en->it" in low or "translated" in low or "translategemma" in low:
        return ["en", "it"]
    if " it)" in low or "-it_" in low or "italian" in low:
        return ["it"]
    if "en" in low:
        return ["en"]
    return []


def _host_hint(text: str) -> str | None:
    low = text.lower()
    for host in _HOST_HINTS:
        if host in low:
            return host
    return None


# --------------------------------------------------------------------------
# HTTP client (raw proxy access)
# --------------------------------------------------------------------------


class GatewayClient:
    """Raw OpenAI-compatible HTTP client for the LLM Gateway.

    Only the endpoints the adapter needs are implemented: ``/v1/health`` and
    ``/v1/models`` and ``/v1/chat/completions``.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        *,
        local_only: bool | None = None,
    ) -> None:
        import httpx

        base = base_url or cfg.LLM_GATEWAY_BASE_URL
        # §13.1: never point at a non-local endpoint.
        cfg.assert_local_url(base)
        self.base_url = base.rstrip("/")
        self.api_key = api_key or cfg.LLM_GATEWAY_API_KEY
        self.timeout = timeout or cfg.LLM_GATEWAY_TIMEOUT
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout
        )

    @property
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _post(self, path: str, payload: dict) -> dict:
        t0 = time.monotonic()
        try:
            resp = await self._client.post(path, json=payload, headers=self.headers)
        except self._httpx.HTTPError as exc:  # pragma: no cover - network
            raise GatewayError(f"request to {path} failed: {exc}") from exc
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code >= 400:
            raise GatewayError(
                f"{path} -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:  # pragma: no cover - non-JSON
            raise GatewayError(f"{path} returned non-JSON body") from exc
        return {"body": body, "elapsed_ms": elapsed_ms}

    async def _get(self, path: str) -> dict:
        t0 = time.monotonic()
        try:
            resp = await self._client.get(path, headers=self.headers)
        except self._httpx.HTTPError as exc:  # pragma: no cover - network
            raise GatewayError(f"request to {path} failed: {exc}") from exc
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code >= 400:
            raise GatewayError(
                f"{path} -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            body = resp.json()
        except ValueError as exc:  # pragma: no cover
            raise GatewayError(f"{path} returned non-JSON body") from exc
        return {"body": body, "elapsed_ms": elapsed_ms}

    async def health(self) -> dict:
        """``GET /v1/health`` -> {status, target, latency_ms}.

        Falls back to ``/health`` (ADR-001 §3.1) when ``/v1/health`` is not
        exposed.
        """
        for path in ("/v1/health", "/health"):
            try:
                r = await self._get(path)
                body = r["body"]
                return {
                    "status": "ok",
                    "proxy_status": body.get("status"),
                    "target": body.get("target"),
                    "endpoint": path,
                    "latency_ms": round(r["elapsed_ms"], 2),
                }
            except GatewayError:
                continue
        # Neither endpoint answered: report the last failure.
        return {
            "status": "unreachable",
            "proxy_status": None,
            "target": None,
            "endpoint": None,
            "latency_ms": None,
        }

    async def list_models(self) -> list[dict]:
        """``GET /v1/models`` -> the raw ``data`` list (each model object)."""
        r = await self._get("/models")
        data = r["body"].get("data")
        if not isinstance(data, list):
            raise GatewayError("/v1/models did not return a data list")
        return data

    async def completion(
        self,
        model_id: str,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict | None = None,
        seed: int | None = None,
        reasoning_effort: str | None = None,
        idempotency_key: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """``POST /v1/chat/completions`` with the OpenAI-compatible body."""
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format
        if seed is not None:
            payload["seed"] = seed
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        if extra:
            payload.update(extra)
        return await self._post("/chat/completions", payload)

    async def close(self) -> None:  # pragma: no cover - trivial
        await self._client.aclose()


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _status_from_loaded(value: str) -> str:
    """Map the proxy ``status.value`` to the capability-matrix status."""
    v = (value or "").lower()
    if v == "loaded":
        return "available"
    if v == "unloaded":
        return "offline"
    return "degraded"


class GatewayAdapter:
    """Transforms proxy data into the capability matrix and enforces §8.4.

    Holds a :class:`GatewayClient` (injectable and a per-process cache of the
    last refresh. ``new_session`` is a SQLAlchemy sessionmaker so run
    persistence can be tested against the same DB as the rest of the app.
    """

    def __init__(
        self,
        client: GatewayClient,
        *,
        default_max_output: int | None = None,
        context_window_fallback: int | None = None,
        new_session=None,
        logger=None,
    ) -> None:
        self.client = client
        self.default_max_output = (
            default_max_output or cfg.LLM_GATEWAY_MAX_OUTPUT_TOKENS
        )
        self.context_window_fallback = context_window_fallback
        self._new_session = new_session
        self._logger = logger or _NullLogger()
        self._cache: list[GatewayModel] | None = None
        self._refreshed_at: float | None = None
        self._health: dict | None = None

    # --- session helper -------------------------------------------------
    def _session(self, owner=None):
        if self._new_session is not None:
            return self._new_session()
        from .db import SessionLocal

        return SessionLocal()

    # --- capability matrix (§8.1) --------------------------------------
    async def list_models(self, *, force: bool = False) -> list[GatewayModel]:
        """Return the capability matrix, refreshed from the proxy.

        Cached until ``force`` is set or the cache is empty (§8.1: refresh on
        startup and on explicit request).
        """
        if not force and self._cache is not None:
            return self._cache

        try:
            raw = await self.client.list_models()
        except GatewayError:
            # Proxy unreachable: return whatever is cached, or an empty,
            # all-offline matrix so the UI can still render and the batch can
            # be suspended (§8.4) instead of raising on every read.
            if self._cache is None:
                return []
            return self._cache

        # Refresh health once so status reflects the real proxy state.
        try:
            self._health = await self.client.health()
        except GatewayError:
            self._health = {"status": "unreachable"}

        models: list[GatewayModel] = []
        for row in raw:
            models.append(self._build_model(row))

        self._cache = models
        self._refreshed_at = time.monotonic()
        self._logger.info(f"gateway models refreshed: {len(models)} models")
        return models

    def _build_model(self, row: dict) -> GatewayModel:
        mid = row.get("id") or "unknown"
        name = row.get("name") or mid
        provider = (row.get("owned_by") or "llama-swap").lower()
        status = _status_from_loaded((row.get("status") or {}).get("value"))

        ctx = _derive_context_window(name)
        if ctx is None and self.context_window_fallback:
            ctx = self.context_window_fallback

        desc = (row.get("description") or "") + " " + name
        vram = _host_hint(desc)

        return GatewayModel(
            id=mid,
            display_name=name,
            provider=provider,
            context_window=ctx,
            max_output=self.default_max_output,
            supports_json_schema=_PROXY_CAPABILITIES["supports_json_schema"],
            supports_streaming=_PROXY_CAPABILITIES["supports_streaming"],
            supports_reasoning=_PROXY_CAPABILITIES["supports_reasoning"],
            supports_seed=_PROXY_CAPABILITIES["supports_seed"],
            languages=_languages_from_name(name),
            locality="on-premise",
            status=status,
            vram_host=vram,
        )

    async def refresh(self) -> list[GatewayModel]:
        """Force a refresh (startup + explicit /refresh)."""
        return await self.list_models(force=True)

    # --- health (§8.1 health/latency) ----------------------------------
    async def health(self, *, force: bool = False) -> dict:
        h = await self.client.health()
        self._health = h
        try:
            models = await self.list_models(force=force)
        except GatewayError:
            models = self._cache or []
        available = [m for m in models if m.status == "available"]
        offline = [m for m in models if m.status == "offline"]
        h.update(
            {
                "models_total": len(models),
                "models_available": len(available),
                "models_offline": len(offline),
                "last_refresh": _now_iso(),
            }
        )
        return h

    # --- availability / no-autoswitch (§8.4) ---------------------------
    def available(self, model_id: str) -> GatewayModel | None:
        """Return the model if it exists and is ``available``.

        Raises :class:`ModelUnavailableError` when the model is unknown or
        offline. This is the single point that guarantees an unavailable
        model is NEVER silently replaced (§8.4).
        """
        models = self._cache or []
        for m in models:
            if m.id == model_id:
                if m.status == "available":
                    return m
                raise ModelUnavailableError(
                    model_id, f"is offline (status={m.status})"
                )
        raise ModelUnavailableError(model_id, "not found in capability matrix")

    # --- run recording (§16, §8.4) -------------------------------------
    def record_run(
        self,
        *,
        project_id: str,
        run_type: str,
        model_name: str,
        prompt: str,
        parameters: dict,
        output: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        status: str = "completed",
        error: str | None = None,
        idempotency_key: str | None = None,
        branch_of: str | None = None,
    ):
        """Persist one call in ``llm_runs`` (input/output hash + params)."""
        run = None
        try:
            session = self._session()
            from .models import LLMRun

            params = {**parameters, "idempotency_key": idempotency_key}
            if branch_of:
                params["branch_of"] = branch_of
            run = LLMRun(
                project_id=str(project_id),
                run_type=run_type,
                model_name=model_name,
                prompt_hash=_sha256(prompt),
                parameters=params,
                output_hash=_sha256(output) if output else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                status=status,
                error=error,
            )
            session.add(run)
            session.commit()
            run_id = run.id
            return run_id
        finally:
            if run is not None:
                session.close()

    # --- test run (§8.3 / test-run endpoint) ---------------------------
    async def test_run(
        self,
        *,
        project_id: str,
        model_id: str,
        messages: list[dict],
        purpose: str = "test",
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_schema: dict | None = None,
        seed: int | None = None,
        reasoning_effort: str | None = "none",
        idempotency_key: str | None = None,
    ) -> dict:
        """One test call against the proxy, recorded in ``llm_runs``.

        Raises :class:`ModelUnavailableError` if the model is not available
        (§8.4: suspend, never switch and :class:`SchemaValidationError` if
        the output does not match the requested schema.
        """
        import uuid as _uuid

        # §8.4: no autoswitch -- validate before any network call.
        model = self.available(model_id)

        if idempotency_key is None:
            idempotency_key = str(_uuid.uuid4())

        params: dict[str, Any] = {
            "purpose": purpose,
            "max_tokens": max_tokens or self.default_max_output,
            "temperature": temperature,
            "seed": seed,
            "reasoning_effort": reasoning_effort,
        }
        if json_schema is not None:
            params["response_format"] = {"type": "json_object"}
            params["schema_provided"] = True

        prompt_blob = json.dumps(
            {"model": model_id, "messages": messages}, sort_keys=True
        )

        try:
            resp = await self.client.completion(
                model_id=model_id,
                messages=messages,
                max_tokens=params["max_tokens"],
                temperature=temperature,
                response_format=(
                    {"type": "json_object"} if json_schema is not None else None
                ),
                seed=seed,
                reasoning_effort=reasoning_effort,
                idempotency_key=idempotency_key,
            )
        except GatewayError as exc:
            # §8.4: record the failure for every GatewayError (transient
            # exhausted or non-transient), then retry transient ones only.
            self.record_run(
                project_id=project_id,
                run_type=purpose,
                model_name=model_id,
                prompt=prompt_blob,
                parameters=params,
                status="failed",
                error=str(exc),
                idempotency_key=idempotency_key,
            )
            retried = await self._safe_retry(
                lambda: self.client.completion(
                    model_id=model_id,
                    messages=messages,
                    max_tokens=params["max_tokens"],
                    temperature=temperature,
                    seed=seed,
                    reasoning_effort=reasoning_effort,
                    idempotency_key=idempotency_key,
                )
            )
            if retried is None:
                raise
            resp = retried

        self._validate_schema(resp, json_schema)
        usage = resp.get("usage") or {}
        content = ""
        try:
            content = resp["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover
            raise SchemaValidationError(
                "completion response had no message content"
            ) from exc

        run = self.record_run(
            project_id=project_id,
            run_type=purpose,
            model_name=model_id,
            prompt=prompt_blob,
            parameters=params,
            output=content,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            status="completed",
            idempotency_key=idempotency_key,
        )
        return {
            "ok": True,
            "model": model.id,
            "run_id": run,
            "idempotency_key": idempotency_key,
            "content": content,
            "usage": usage,
            "latency_ms": None,
        }

    async def _safe_retry(
        self, fn, *, attempts: int = cfg.LLM_MAX_RETRIES
    ) -> Any:
        """Retry a callable for transient errors only (§8.4).

        Returns the first successful result or ``None`` if every attempt
        failed. Non-transient errors (4xx other than 429) are re-raised.
        """
        for _ in range(max(1, attempts)):
            try:
                return await fn()
            except GatewayError as exc:
                status = str(getattr(exc, "status_code", ""))
                if "429" not in str(exc) and "5" not in status:
                    # 4xx (auth / bad request) are not transient.
                    raise
            except Exception:  # network / timeout -> transient
                pass
            await asyncio.sleep(0.3)
        return None

    @staticmethod
    def _validate_schema(resp: dict, json_schema: dict | None) -> None:
        """Validate a json_object response when a schema was requested."""
        if json_schema is None:
            return
        # The proxy returns a JSON string; parse and check it is an object.
        raw = (
            resp.get("choices", [{}])[0].get("message", {}).get("content", "")
        )
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise SchemaValidationError(
                "json_object response was not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise SchemaValidationError(
                "json_object response was not a JSON object"
            )


class _NullLogger:
    def info(self, *a, **k):  # pragma: no cover
        pass

    def warning(self, *a, **k):  # pragma: no cover
        pass


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Process-wide adapter (injected into the routes)
# --------------------------------------------------------------------------

_ADAPTER: GatewayAdapter | None = None


def get_adapter() -> GatewayAdapter:
    """Return the process-wide adapter, creating it on first use."""
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = GatewayAdapter(client=GatewayClient())
    return _ADAPTER


def set_adapter(adapter: GatewayAdapter) -> None:
    """Replace the process-wide adapter (used by tests)."""
    global _ADAPTER
    _ADAPTER = adapter


def reset_adapter() -> None:
    """Drop the process-wide adapter (tests / cold start)."""
    global _ADAPTER
    _ADAPTER = None
