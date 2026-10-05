"""Standalone QE (quality estimation) server for Trans.

Exposes the OpenAI-compatible surface that ``backend.backend.qe_client``
expects, backed by ANY OpenAI-compatible LLM endpoint:

* ``GET  /health``              -> liveness + configuration summary
* ``POST /v1/chat/completions`` -> JSON-constrained QE completion

Run it when your QE model lives on a plain llama.cpp / vLLM / LM Studio
server and you do not want to expose it as a gateway upstream:

    UPSTREAM_BASE_URL=http://127.0.0.1:8080/v1 \
    UPSTREAM_MODEL=my-qe-model \
    uvicorn app:app --host 127.0.0.1 --port 8081

then point the platform at it:

    QE_BASE_URL=http://127.0.0.1:8081
    QE_MODEL=my-qe-model

Configuration (environment variables):

* ``UPSTREAM_BASE_URL`` -- OpenAI-compatible base URL of the backing server
  (default ``http://127.0.0.1:8080/v1``).
* ``UPSTREAM_API_KEY``  -- bearer token sent upstream (default
  ``sk-local-dev-change-me``).
* ``UPSTREAM_MODEL``    -- model id used for completions (default
  ``qe-verify``).
"""
from __future__ import annotations

import os
import time

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:8080/v1")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "sk-local-dev-change-me")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "qe-verify")
TIMEOUT = float(os.getenv("QE_UPSTREAM_TIMEOUT", "180"))

SYSTEM = (
    "You are a meticulous translation quality inspector for "
    "English-to-Italian literary translation. You always answer with a "
    "single JSON object and nothing else."
)

USER_TEMPLATE = """SOURCE (original English):
<source>
{source}
</source>

TRANSLATION (claimed to be Italian):
<translation>
{target}
</translation>

Evaluate the pair and answer with one JSON object with exactly these keys:
- "is_italian": probability from 0.0 to 1.0 that the TRANSLATION text is written in the Italian language
- "is_translated": probability from 0.0 to 1.0 that the TRANSLATION is a complete and faithful Italian rendering of the SOURCE (1.0 = faithful and complete; 0.0 = unrelated text, missing or untranslated parts)
- "is_english": probability from 0.0 to 1.0 that the TRANSLATION text is actually English, i.e. it was not translated at all

Remember: answer only with the JSON object, for example {{"is_italian": 0.95, "is_translated": 0.9, "is_english": 0.02}}"""

app = FastAPI(title="Trans QE service", version="1.0.0")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    temperature: float | None = 0.0
    max_tokens: int | None = 120


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "qe",
        "model": UPSTREAM_MODEL,
        "upstream": UPSTREAM_BASE_URL,
    }


def _extract_pair(messages: list[ChatMessage]) -> tuple[str, str]:
    """Pull (source, target) back out of the QE prompt sent by the client."""
    user = "\n".join(m.content for m in messages if m.role == "user")
    try:
        source = user.split("<source>")[1].split("</source>")[0].strip()
        target = user.split("<translation>")[1].split("</translation>")[0].strip()
    except IndexError:
        raise HTTPException(status_code=422, detail="prompt is not a QE prompt")
    return source, target


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest) -> dict:
    source, target = _extract_pair(req.messages)
    payload = {
        "model": req.model or UPSTREAM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_TEMPLATE.format(
                source=source, target=target)},
        ],
        "temperature": req.temperature or 0.0,
        "max_tokens": req.max_tokens or 120,
    }
    if req.model is None:
        # Ask the backing server to constrain the output to a JSON object
        # when it understands the llama.cpp-style response_format.
        payload["response_format"] = {"type": "json_object"}
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            r = httpx.post(
                f"{UPSTREAM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {UPSTREAM_API_KEY}"},
                json=payload,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            choice = (r.json().get("choices") or [{}])[0]
            return {
                "id": f"qe-{int(time.time() * 1000)}",
                "object": "chat.completion",
                "model": payload["model"],
                "choices": [{"index": 0,
                             "message": {"role": "assistant",
                                         "content": choice.get("message", {})
                                         .get("content", "")},
                             "finish_reason": "stop"}],
            }
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(5)  # give a cold-loading upstream a second chance
    raise HTTPException(status_code=502, detail=f"upstream failed: {last_error}")
