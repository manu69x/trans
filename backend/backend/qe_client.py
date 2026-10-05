"""Segment-level translation quality estimation (QE) client.

Asks an LLM endpoint to score a translated segment on three 0..1
probabilities, produced with a single JSON-constrained chat completion:

* ``is_italian``    -- probability the target text is written in Italian
* ``is_translated`` -- probability the target is a complete and faithful
  Italian rendering of the source
* ``is_english``    -- probability the target is actually still English
  (i.e. it was never translated)

Configuration:

* ``QE_BASE_URL``  -- OpenAI-compatible base URL of the QE endpoint. When
  unset it is derived from ``LLM_GATEWAY_BASE_URL`` (the gateway's
  ``/upstream/qe`` route), so a single gateway can serve both translation
  and verification models. See ``deploy/qe-service/README.md``.
* ``QE_MODEL``     -- model id sent in the completion payload
  (default ``qe-verify``).
* ``LLM_GATEWAY_API_KEY`` -- bearer token forwarded to the endpoint.

The public signature ``verify_segment_qe(source, target)`` is stable by
design: handlers, DB fields and the frontend depend on it.
"""
from __future__ import annotations

import json
import os

import httpx

from .config import LLM_GATEWAY_API_KEY, LLM_GATEWAY_BASE_URL, QE_BASE_URL

VERIFY_TIMEOUT = 180.0
_MODEL = os.getenv("QE_MODEL", "qe-verify")

_SYSTEM = (
    "You are a meticulous translation quality inspector for "
    "English-to-Italian literary translation. You always answer with a "
    "single JSON object and nothing else."
)

_USER_TEMPLATE = """SOURCE (original English):
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


def _base_url() -> str:
    base = (QE_BASE_URL or LLM_GATEWAY_BASE_URL).rstrip("/")
    return base.removesuffix("/v1")


def _headers() -> dict:
    return {"Authorization": f"Bearer {LLM_GATEWAY_API_KEY}"}


def _clamp(value) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _extract_probabilities(content: str) -> dict:
    """The model may wrap the JSON in fences or prose: recover and clamp."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in verifier reply: {text[:120]}")
    obj = json.loads(text[start:end + 1])
    return {
        "is_italian": _clamp(obj.get("is_italian")),
        "is_translated": _clamp(obj.get("is_translated")),
        "is_english": _clamp(obj.get("is_english")),
    }


def verify_segment_qe(source: str, target: str) -> dict:
    """Score one (English source, Italian target) pair; returns 0..1 probs.

    A single call: the prompt asks for all three judgements at once and the
    endpoint is expected to constrain the output to valid JSON (e.g.
    ``response_format: json_object`` on llama.cpp-based servers).
    """
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER_TEMPLATE.format(
                source=source, target=target)},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            r = httpx.post(
                f"{_base_url()}/v1/chat/completions",
                headers=_headers(),
                json=payload,
                timeout=VERIFY_TIMEOUT,
            )
            r.raise_for_status()
            content = (r.json().get("choices") or [{}])[0].get(
                "message", {}).get("content")
            return _extract_probabilities(content)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            last_error = exc
    raise RuntimeError(f"QE verifier failed: {last_error}")


def health() -> dict:
    r = httpx.get(f"{_base_url()}/health", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()
