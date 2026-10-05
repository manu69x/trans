"""Gateway adapter routes (PRD §8 / ADR-001).

Endpoints (all under ``/api/gateway``):

* ``GET  /api/gateway/models``   -- capability matrix (§8.1), refreshed from
  the real proxy.
* ``GET  /api/gateway/health``   -- proxy health (§8.1 health/latency).
* ``POST /api/gateway/test-run`` -- one test call with schema (§8.3), recorded
  in ``llm_runs`` (§16 / §8.4).
* ``POST /api/gateway/refresh``  -- force a refresh (§8.1).

The routes never hardcode model names: every value comes from the proxy
(``/api/gateway/models``) or the ADR-001-measured proxy capabilities.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import gateway as gateway_mod  # noqa: F401 (kept for symmetry)
from .gateway import (
    ModelUnavailableError,
    GatewayError,
    SchemaValidationError,
    get_adapter,
)
from .rbac import require_permission

router = APIRouter(prefix="/gateway", tags=["gateway"])


class _RunRequest(BaseModel):
    """Request body for ``POST /api/gateway/test-run`` (mirrors §8.3)."""

    project_id: str
    model_id: str
    system_prompt: str | None = None
    user_prompt: str | None = None
    messages: list[dict] | None = None
    json_schema: dict | None = None
    purpose: str = "translation"
    temperature: float | None = None
    max_output: int | None = None
    seed: int | None = None
    reasoning_effort: str | None = "none"
    idempotency_key: str | None = None

    def build_messages(self) -> list[dict]:
        """Assemble the message list from the request.

        Accepts either an explicit ``messages`` list or ``system``/``user``
        prompts (the §8.3 contract).
        """
        if self.messages:
            return self.messages
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if self.user_prompt:
            messages.append({"role": "user", "content": self.user_prompt})
        if not messages:
            messages.append(
                {
                    "role": "user",
                    "content": "Health check via /api/gateway/test-run",
                }
            )
        return messages


@router.get("/models")
async def list_models() -> dict:
    """Capability matrix (§8.1).

    Refreshed from the real proxy on each call unless a refresh is already in
    the process cache. No model names are hardcoded.
    """
    try:
        models = await get_adapter().list_models()
    except GatewayError as exc:
        raise HTTPException(
            status_code=503, detail=f"Gateway proxy unreachable: {exc}"
        ) from exc
    return {
        "count": len(models),
        "models": [m.to_dict() for m in models],
    }


@router.get("/health")
async def health() -> dict:
    """Proxy health (§8.1 health/latency)."""
    try:
        return await get_adapter().health()
    except GatewayError as exc:
        raise HTTPException(
            status_code=503, detail=f"Gateway proxy unreachable: {exc}"
        ) from exc


@router.post("/refresh", dependencies=[Depends(require_permission("config_gateway"))])
async def refresh() -> dict:
    """Force a refresh (§8.1)."""
    try:
        models = await get_adapter().refresh()
    except GatewayError as exc:
        raise HTTPException(
            status_code=503, detail=f"Gateway proxy unreachable: {exc}"
        ) from exc
    return {"status": "ok", "count": len(models)}


@router.post("/test-run", response_model=None, dependencies=[Depends(require_permission("config_gateway"))])
async def test_run(body: _RunRequest) -> dict:
    """One test call with schema (§8.3 / §8.4).

    Records the run in ``llm_runs`` (§16) and raises ``ModelUnavailableError``
    (mapped to 409) when the model is not available -- never switching it
    automatically (§8.4).
    """
    try:
        result = await get_adapter().test_run(
            project_id=body.project_id,
            model_id=body.model_id,
            messages=body.build_messages(),
            purpose=body.purpose,
            max_tokens=body.max_output,
            temperature=body.temperature,
            json_schema=body.json_schema,
            seed=body.seed,
            reasoning_effort=body.reasoning_effort,
            idempotency_key=body.idempotency_key,
        )
    except ModelUnavailableError as exc:
        # §8.4: suspend the batch and ask the user; never auto-replace.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "model_unavailable",
                "model_id": exc.model_id,
                "reason": exc.reason,
                "action": "suspend_batch_and_ask_user",
            },
        ) from exc
    except SchemaValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except GatewayError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
