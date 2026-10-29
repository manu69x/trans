"""Client di verifica traduzioni (Spark-X2.5-4B su GPU0, via llama-swap).

Catena (ADR-008 pattern): Gateway :8080 -> llama-swap :8088 ->
/upstream/qe-verify -> llama-server (GGUF Spark-X2.5-4B-GSQ-RCO).

Sostituisce Open-QE (2026-09-24): QE era addestrato solo su inglese
(banking77/sst5/boolq) e non sapeva giudicare lingue ne' coppie di
traduzione — le sue probabilita' erano rumore (incidente segmento 347).

Il generalista Spark non ha una testa calibrata come QE, quindi le tre
probabilita' si ottengono per via promptistica: una singola chiamata
chat-completions con output vincolato a JSON (response_format json_object
-> grammatica GBNF di llama-server) e tre chiavi 0..1:

* ``is_target_lang`` -- probabilita' che il target sia nella lingua target
  del progetto (2026-10-29, multilingua Tier 1: prima ``is_italian``)
* ``is_translated``  -- probabilita' che il target renda fedelmente e
  completamente il sorgente
* ``is_source_lang`` -- probabilita' che il target sia in realta' nella
  lingua di partenza (prima ``is_english``: bozza non tradotta)

La coppia lingue arriva dal progetto (source/target_language, catalogo
``backend.languages``); default en→it per retrocompatibilita'.
"""
from __future__ import annotations

import json

import httpx

from .config import LLM_GATEWAY_API_KEY, LLM_GATEWAY_BASE_URL
from .languages import language_name

VERIFY_TIMEOUT = 180.0
_MODEL = "qe-verify"

_VERIFY_KEYS = ("is_target_lang", "is_translated", "is_source_lang")


def _system_prompt(source_lang: str, target_lang: str) -> str:
    return (
        "You are a meticulous translation quality inspector for "
        f"{language_name(source_lang)}-to-{language_name(target_lang)} "
        "literary translation. You always answer with a "
        "single JSON object and nothing else."
    )


def _user_template(source: str, target: str,
                   source_lang: str, target_lang: str) -> str:
    src_name = language_name(source_lang)
    tgt_name = language_name(target_lang)
    return f"""SOURCE (original {src_name}):
<source>
{source}
</source>

TRANSLATION (claimed to be {tgt_name}):
<translation>
{target}
</translation>

Evaluate the pair and answer with one JSON object with exactly these keys:
- "is_target_lang": probability from 0.0 to 1.0 that the TRANSLATION text is written in the {tgt_name} language
- "is_translated": probability from 0.0 to 1.0 that the TRANSLATION is a complete and faithful {tgt_name} rendering of the SOURCE (1.0 = faithful and complete; 0.0 = unrelated text, missing or untranslated parts)
- "is_source_lang": probability from 0.0 to 1.0 that the TRANSLATION text is actually {src_name}, i.e. it was not translated at all

Remember: answer only with the JSON object, for example {{"is_target_lang": 0.95, "is_translated": 0.9, "is_source_lang": 0.02}}"""


def _base_url() -> str:
    base = LLM_GATEWAY_BASE_URL.rstrip("/")
    return base.removesuffix("/v1") + "/upstream/qe-verify"


def _headers() -> dict:
    return {"Authorization": f"Bearer {LLM_GATEWAY_API_KEY}"}


def _clamp(value) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 4)
    except (TypeError, ValueError):
        return 0.0


def _extract_probabilities(content: str) -> dict:
    """Il modello puo' imbrattare il JSON con fence/prosa: recupera e clamp."""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in verifier reply: {text[:120]}")
    obj = json.loads(text[start:end + 1])
    return {key: _clamp(obj.get(key)) for key in _VERIFY_KEYS}


def verify_segment_qe(source: str, target: str, *,
                       source_lang: str = "en",
                       target_lang: str = "it") -> dict:
    """Le tre probabilita' per una coppia (sorgente, target), 0..1.

    Una sola chiamata: il prompt chiede tutte e tre le valutazioni insieme
    e llama-server vincola l'output a JSON valido (grammatica GBNF).
    """
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system",
             "content": _system_prompt(source_lang, target_lang)},
            {"role": "user", "content": _user_template(
                source, target, source_lang, target_lang)},
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
    raise RuntimeError(f"spark verifier failed: {last_error}")


def health() -> dict:
    r = httpx.get(f"{_base_url()}/health", headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()
