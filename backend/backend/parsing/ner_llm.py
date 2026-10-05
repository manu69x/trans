"""Pure LLM-structured NER layer (PRD §6.2.3, §6.3, §8, §9.5).

Takes the candidates already proposed by BookNLP / transformer NER, selects
the **high-ambiguity** ones and the **§6.3 domain categories** (alien
species, artefacts, curses, fictional institutions) the local models cover
weakly, and builds schema-constrained classification blocks for the
analysis model through LLM Gateway (§8.2). No DB access and no network
here: the runner (:mod:`backend.llm_ner_runner`) feeds candidates and
persists the results — the network call lives in :mod:`backend.gateway_http`.

Design (PRD-anchored):
* §6.2.3 — LLM only for high-ambiguity candidates and domain categories;
  never the primary segmentation, never a rewrite of BookNLP output.
* §6.2.4/§6.6 — every LLM proposal is a *proposta revisionabile*: it lands
  as evidence with ``extractor='llm'``; nothing overwrites BookNLP rows.
* §8.2 — analysis model, temperature 0–0.2, JSON-constrained output.
* §9.5 — output validated against the JSON Schema; per-call failures are
  recorded (``schema_error``) and the run continues.
* Idempotency per block (§13.2): the prompt hash keys an ``llm_runs`` row;
  a re-run of the same block on the same text is a cache hit.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# §6.2.3 scope: which candidates go to the LLM
# ---------------------------------------------------------------------------
# Domain categories the §6.2.3 lists explicitly.
DOMAIN_CATEGORIES: tuple[str, ...] = (
    "CREATURE_SPECIES",     # specie aliene (vampire, Wyrm, Martian)
    "OBJECT_ARTIFACT",      # artefatti (Black Key, Time Engine)
    "CURSE",                # maledizioni (§6.2.3; nota anche in §6.3 note)
    "ORG_FACTION",          # istituzioni fittizie (The Order, NASA)
)

# Confidence under which a candidate is "high ambiguity" (the deterministic
# scorer of parsing/entities.py discounts NOM/PRON mentions and few-evidence
# clusters; see compute_confidence there).
HIGH_AMBIGUITY_THRESHOLD = 0.55

# An article-initial "PERSON"/"LOCATION" (canonical form starting with "the ")
# is ambiguous *by surface*: the deterministic tagger routinely mints PERSON
# rows for §6.2.3 domain concepts ("the Withering" → curse, "the Pale Hand"
# → faction). They always go to the LLM for review evidence (§6.6) — the
# field-level merge still never re-types them (§6.2.3). Deliberately
# confidence-independent: scope membership must be stable across re-runs,
# otherwise the LLM's own confidence bump would reshuffle blocks and break
# per-block idempotency (§13.2).
AMBIGUOUS_SURFACE_PREFIXES: tuple[str, ...] = ("the ",)

# Weak default types: the fallback buckets of the deterministic pipeline.
# A candidate carrying one of them is ambiguous *by type* (the models could
# not decide what it is) — exactly the §6.2.3 "alta ambiguità" scope, even
# when its mention confidence is high. Also the only types the LLM may
# re-type during the merge.
WEAK_DEFAULT_TYPES: frozenset[str] = frozenset(
    {"CONCEPT_TERM", "WORK_MEDIA", "", "UNKNOWN"})

# Any candidate can be re-typed into these (validated against §6.3 categories
# the entity surface understands; entity_routes' list may grow independently).
LLM_ALLOWED_TYPES: tuple[str, ...] = (
    "PERSON", "ROLE", "CREATURE_SPECIES", "OBJECT_ARTIFACT", "LOCATION",
    "ORG_FACTION", "WORK_MEDIA", "EVENT", "CONCEPT_TERM", "CURSE",
)

# Evidence an LLM classification must quote back (anti-hallucination guard,
# §13.4 risk table: grounded proposals only).
MIN_EVIDENCE_CHARS = 4

# ---------------------------------------------------------------------------
# JSON Schema of one classification block (§9.5: grammar-constrained output)
# ---------------------------------------------------------------------------
CLASSIFICATION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications"],
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "category", "confidence", "evidence_quote"],
                "properties": {
                    "id": {"type": "string"},
                    "category": {"enum": list(LLM_ALLOWED_TYPES)},
                    "confidence": {"type": "number",
                                   "minimum": 0.0, "maximum": 1.0},
                    "evidence_quote": {"type": "string"},
                    "definition": {"type": "string"},
                },
            },
        },
    },
}


@dataclass
class LLMCandidate:
    """One candidate the LLM may classify (from BookNLP/HF proposals)."""

    id: str                      # stable: "<source>:<norm-name-hash>"
    name: str                    # canonical surface form
    current_type: str            # BookNLP/HF §6.3 category
    confidence: float | None     # deterministic confidence (entities.py)
    mention_count: int
    quotes: list[str] = field(default_factory=list)   # up to 3 contexts


@dataclass
class Classification:
    """One validated LLM proposal for a candidate."""

    candidate_id: str
    name: str
    category: str            # validated against LLM_ALLOWED_TYPES
    confidence: float
    evidence_quote: str
    definition: str | None
    schema_valid: bool


@dataclass
class BlockResult:
    """Outcome of one structured LLM call (one block)."""

    block_hash: str
    prompt_sha256: str
    status: str               # classified | invalid_json | http_error | empty
    classifications: list[Classification] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Candidate selection (§6.2.3: only high-ambiguity / domain candidates)
# ---------------------------------------------------------------------------
_CITE_MARK = re.compile(r"[“”«»]")


def select_candidates(
    entities: list[dict],
    text: str,
    max_candidates: int = 128,
) -> list[LLMCandidate]:
    """Filter proposed entities down to the §6.2.3 LLM scope.

    *entities* are read-only dicts (``canonical_source``, ``entity_type``,
    ``confidence``, ``mention_count``) as produced by the F2 extraction.
    A candidate qualifies when its category is a §6.2.3 domain category, or
    its deterministic confidence is under :data:`HIGH_AMBIGUITY_THRESHOLD`
    (candidato ad alta ambiguità). The isolated first-person ``I`` never
    qualifies (§5.5 hard rule, already enforced upstream). *text* supplies
    up to three quote contexts per candidate (§15.2 evidence grounding).
    """
    from .entities import _normalise_name
    from .preprocess import is_isolated_I

    low = text.lower()
    candidates: list[LLMCandidate] = []
    for entity in entities:
        name = (entity.get("canonical_source") or "").strip()
        if not name or is_isolated_I(name):
            continue
        etype = entity.get("entity_type") or ""
        confidence = entity.get("confidence")
        conf = float(confidence) if confidence is not None else None
        lowered = name.lower()
        surface_ambiguous = (
            lowered.startswith(AMBIGUOUS_SURFACE_PREFIXES)
            and etype in {"PERSON", "LOCATION"}
        )
        ambiguous = (
            conf is None
            or conf < HIGH_AMBIGUITY_THRESHOLD
            or etype in WEAK_DEFAULT_TYPES  # ambiguous *by type*
            or surface_ambiguous            # ambiguous *by surface* (§6.2.3)
        )
        if etype not in DOMAIN_CATEGORIES and not ambiguous:
            continue
        quotes = _find_quotes(low, name)
        if not quotes:
            continue  # nothing citable in this chapter → not classifiable
        digest = hashlib.sha256(
            _normalise_name(name).encode("utf-8")).hexdigest()[:12]
        candidates.append(LLMCandidate(
            # Deliberately type-independent and confidence-independent:
            # the id must survive the LLM's own re-typing / confidence
            # bump so a re-run recognises the candidate as already
            # classified (per-candidate idempotency, §13.2).
            id=f"cand:{digest}",
            name=name,
            current_type=etype or "CONCEPT_TERM",
            confidence=conf,
            mention_count=int(entity.get("mention_count") or 0),
            quotes=quotes,
        ))
        if len(candidates) >= max_candidates:
            break
    return candidates


def _find_quotes(low_text: str, name: str, limit: int = 3) -> list[str]:
    """Up to *limit* ±60-char contexts of *name* (word-boundary, naive)."""
    quotes: list[str] = []
    needle = name.lower()
    start = 0
    while len(quotes) < limit:
        idx = low_text.find(needle, start)
        if idx < 0:
            break
        end = idx + len(needle)
        boundary_ok = (
            (idx == 0 or not (low_text[idx - 1].isalnum()))
            and (end >= len(low_text) or not low_text[end].isalnum())
        )
        start = end
        if not boundary_ok:
            continue
        lo, hi = max(0, idx - 60), min(len(low_text), end + 60)
        quote = " ".join(low_text[lo:hi].split())
        if quote:
            quotes.append(quote)
    return quotes


# ---------------------------------------------------------------------------
# Block building (rate limiting / idempotency unit — one call per block)
# ---------------------------------------------------------------------------
MAX_CANDIDATES_PER_BLOCK = 12


def build_block(
    candidates: list[LLMCandidate], chapter_title: str | None = None
) -> dict:
    """One schema-constrained classification block (system/user prompts).

    Returns ``{"block_hash", "prompt_sha256", "system", "user",
    "candidate_ids"}``. The ``block_hash`` (sha256 over the candidate ids in
    order) is the idempotency key (§13.2); ``prompt_sha256`` hashes the full
    user prompt — the DB stores hashes only (§8.5: riproducibilità senza
    loggare il manoscritto).
    """
    listing = []
    for candidate in candidates:
        entry = {
            "id": candidate.id,
            "name": candidate.name,
            "current_type": candidate.current_type,
            "examples": candidate.quotes,
        }
        listing.append(entry)
    digest_src = "\n".join(c.id for c in candidates)
    block_hash = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]
    user = (
        "You are classifying candidate entities extracted from an English "
        "fantasy/literary novel for a professional EN→IT translator.\n"
        + (f"Chapter: {chapter_title}\n" if chapter_title else "")
        + "For EACH candidate below decide the most likely category.\n"
        "Categories: " + ", ".join(LLM_ALLOWED_TYPES) + "\n"
        "Rules:\n"
        "- CURSE is a named curse, spell or malediction (e.g. 'the "
        "Withering').\n"
        "- CREATURE_SPECIES is a kind/race of being (vampire, Wyrm), not an "
        "individual.\n"
        "- OBJECT_ARTIFACT is a specific named object (a key, an engine).\n"
        "- ORG_FACTION is an institution, order, council or faction.\n"
        "- Quote back the exact sentence fragment that justifies your choice "
        "in evidence_quote (copy it from the examples).\n"
        "- confidence in [0,1]; use it to express how certain you are.\n"
        "- Answer for every id. Output JSON only, following the schema.\n"
        'The JSON object must have exactly this shape: {"classifications": '
        '[{"id": "<candidate id>", "category": "<category>", '
        '"confidence": <number 0..1>, "evidence_quote": "<fragment>", '
        '"definition": "<optional short gloss>"}]}. '
        "Output the raw JSON object only: no markdown fences, no prose.\n\n"
        "Candidates:\n" + json_dumps(listing)
    )
    system = (
        "You are a precise literary NLP annotator. You output only JSON "
        "matching the given schema. Never invent entities beyond the ids "
        "provided."
    )
    prompt_sha = hashlib.sha256(
        (system + "\x1e" + user).encode("utf-8")).hexdigest()
    return {
        "block_hash": block_hash,
        "prompt_sha256": prompt_sha,
        "system": system,
        "user": user,
        "candidate_ids": [c.id for c in candidates],
    }


def json_dumps(obj) -> str:
    """Compact JSON for prompts (kept tiny; no manuscript in logs)."""
    import json
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def chunk_blocks(
    candidates: list[LLMCandidate],
    chapter_title: str | None = None,
    per_block: int = MAX_CANDIDATES_PER_BLOCK,
) -> list[dict]:
    """Split candidates into ≤*per_block* blocks (call unit + rate limit)."""
    return [
        build_block(candidates[i:i + per_block], chapter_title)
        for i in range(0, len(candidates), per_block)
    ]


# ---------------------------------------------------------------------------
# Response validation + field-level merge (§6.2.3: nessuna sovrascrittura)
# ---------------------------------------------------------------------------
def parse_response(
    block: dict,
    candidates_by_id: dict[str, LLMCandidate],
    payload: dict | None,
) -> list[Classification]:
    """Validate one LLM payload against the schema; return valid classes.

    Per-candidate validation (§9.5): a candidate whose entry breaks the
    schema is skipped, the rest survive; a completely invalid payload
    returns ``[]`` and the caller marks the run ``schema_error``. The name
    is re-taken from the candidate table so the LLM cannot inject surfaces.
    """
    import jsonschema

    if not isinstance(payload, dict):
        return []
    try:
        jsonschema.validate(payload, CLASSIFICATION_SCHEMA)
    except jsonschema.ValidationError:
        return []
    out: list[Classification] = []
    for item in payload.get("classifications", []):
        cid = item.get("id")
        candidate = candidates_by_id.get(cid)
        if candidate is None:
            continue
        quote = (item.get("evidence_quote") or "").strip()
        if len(quote) < MIN_EVIDENCE_CHARS:
            continue
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            continue
        out.append(Classification(
            candidate_id=cid,
            name=candidate.name,
            category=item["category"],
            confidence=max(0.0, min(1.0, confidence)),
            evidence_quote=quote,
            definition=(item.get("definition") or "").strip() or None,
            schema_valid=True,
        ))
    return out


def merge_field_level(
    existing: dict | None,
    classification: Classification,
) -> dict:
    """Field-level merge of an LLM proposal into an entity patch (§6.2.3).

    Documented rules (the acceptance test pins them):

    1. ``entity_type`` — LLM wins **only** when the current type is a weak
       default (``CONCEPT_TERM``, ``WORK_MEDIA``, unknown/empty) or the
       candidate qualified through low confidence; a BookNLP ``PERSON`` /
       ``LOCATION`` is never re-typed.
    2. ``definition`` — LLM fills it only when empty (the user's field
       wins, §15.2).
    3. ``confidence`` — combined as ``0.5*llm + 0.5*current`` (evidence
       from two extractors is stronger than either alone).
    4. Everything else — untouched (gender/policy belong to BookNLP+user).
    """
    existing = existing or {}
    current_type = existing.get("entity_type") or ""

    patch: dict = {}
    if current_type in WEAK_DEFAULT_TYPES:
        patch["entity_type"] = classification.category

    if not (existing.get("definition") or "").strip():
        if classification.definition:
            patch["definition"] = classification.definition

    current_conf = existing.get("confidence")
    llm_conf = classification.confidence
    if current_conf is None:
        merged = llm_conf
    else:
        merged = 0.5 * llm_conf + 0.5 * float(current_conf)
    patch["confidence"] = round(min(1.0, merged), 3)

    kept = {k: existing[k] for k in ("entity_type", "definition", "confidence")
            if k in existing}
    return {"patch": patch, "kept": kept, "changed": patch != {} and
            any(existing.get(k) != v for k, v in patch.items())}
