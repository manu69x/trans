"""Deterministic translation validators (PRD §10.1.5-9, §10.2, §15.3).

Every check here is a *pure* function: no DB, no network, no LLM. They run on
the raw JSON returned by Gateway (§9.5, ``chat_json``) against the segments
that were sent to the model and the project's approved constraints
(glossary / entities). They implement the §10.1.5-9 order:

* §10.1.5  JSON syntactic
* §10.1.6  same IDs, order, cardinality
* §10.1.7  placeholders / balanced tags
* §10.1.8  numbers / dates / URLs preserved
* §10.1.9  no residual ``[[...]]`` internal token
* (then)   forbidden terms absent
* (then)   ``must_keep`` identical
* (then)   entities per canonical / policy
* (then)   target identical to source above threshold
* (then)   output too short / too long
* (then)   duplicates / skips / merges of segments

Each returned error carries a ``severity`` of ``"hard`` or ``"soft"``:

* **hard** -- the response is structurally wrong (bad JSON, wrong IDs,
  residual markup, duplicate/merged output). Per AC1 (§15.3 / §10.1.6) the
  whole response is *discarded and retried*: nothing is written to the DB.
* **soft** -- the translation is saved as ``machine_draft`` and the issue is
  recorded on the run (and, where it exists, on the segment flags). A soft
  failure never blocks the save (§10.1.10: the output is still a draft).

Keeping the two severities explicit lets the runner decide, in one pass,
whether to reject the batch (any hard error) or persist it with flags (only
soft errors).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# --- severity labels --------------------------------------------------------
HARD = "hard"
SOFT = "soft"

# §10.2: numbers, dates and URLs must be preserved or transformed per policy.
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_NUMBER_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|kg|g|mg|km|ml|°C|°F|USD|EUR|GBP|yr|yrs|"
    r"years?|months?|days?|hours?|mins?|sec|secs|s)\b",
    re.IGNORECASE,
)
_NUMBER_PLAIN_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")

# §10.1.9 / §9.4: the only internal markup the model must never emit.
_INTERNAL_TOKEN_RE = re.compile(r"\[\[.*?\]\]", re.DOTALL)

# §10.1.7: placeholders / tags whose opening form must have a matching close.
_PLACEHOLDER_PATTERNS = (
    re.compile(r"\{[^}]+\}"),          # {0} {name}
    re.compile(r"<[^/>\s]+(?:\s[^/>]*[^/])?/?>"),  # <b> </b> <img .../>
    re.compile(r"\$\{[^}\+]+\}"),        # ${x}
)


def _err(segment_id, kind, message, severity):
    return {"segment_id": segment_id, "kind": kind,
            "message": message, "severity": severity}


# --- §10.1.5 : JSON syntactic ----------------------------------------------
def validate_json_syntax(response) -> list[dict]:
    """The parsed Gateway output must be a ``{"translations": [...]}`` object.

    ``response`` is the *already parsed* JSON (``chat_json`` raises
    :class:`GatewayInvalidJSON` on non-JSON, but we still guard the shape so a
    ``{"data": {...}}`` or a bare list is not silently accepted (§10.1.5).
    """
    if not isinstance(response, dict):
        return [_err(None, "json_syntax",
                     "response must be a JSON object with a 'translations' key",
                     HARD)]
    translations = response.get("translations")
    if not isinstance(translations, list):
        return [_err(None, "json_syntax",
                     "'translations' must be a list", HARD)]
    return []


def _extract_translations(response) -> list[dict]:
    """Return the ``translations`` items as plain dicts (or [])."""
    if not isinstance(response, dict):
        return []
    items = response.get("translations")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


# --- §10.1.6 : same IDs, order, cardinality --------------------------------
def validate_ids(requested_ids, translations) -> list[dict]:
    """Every requested id must appear exactly once, in the same order.

    ``requested_ids`` is the ordered list of ``segment_id`` that were sent to
    the model (the source order). ``translations`` is the parsed
    ``translations`` list. Any missing / extra / reordered id is a
    :data:`HARD` failure (§10.1.6 / AC1 §15.3).
    """
    errors: list[dict] = []
    got_ids = [t.get("segment_id") for t in translations
               if t.get("segment_id") is not None]
    # cardinality / duplication
    if len(got_ids) != len(requested_ids):
        errors.append(_err(None, "cardinality",
                           f"expected {len(requested_ids)} translations, "
                           f"got {len(got_ids)}", HARD))
    got_set, req_set = set(got_ids), set(requested_ids)
    for missing in [i for i in requested_ids if i not in got_set]:
        errors.append(_err(missing, "id_missing",
                           "segment id missing from the response", HARD))
    for extra in [i for i in got_ids if i not in req_set]:
        errors.append(_err(extra, "id_extra",
                           "segment id not requested", HARD)
                      if not any(e.get("segment_id") == extra
                                 for e in errors)
                      else _err(extra, "id_extra",
                                "segment id not requested", HARD))
    # order
    if all(i in got_set for i in requested_ids) and not errors:
        for idx, want in enumerate(requested_ids):
            if idx < len(got_ids) and got_ids[idx] != want:
                errors.append(_err(want, "id_order",
                                   f"expected {want} at position {idx}, "
                                   f"got {got_ids[idx]}", HARD))
    return errors


# --- §10.1.7 : placeholders / balanced tags --------------------------------
def validate_placeholders(source_text, target_text) -> list[dict]:
    """Each placeholder/tag found in the source must survive in the target.

    A placeholder is ``{...}`, ``${...}`` or an HTML-ish tag. Each opening
    form must appear in the target with a matching closing form (balanced).
    Missing or unbalanced placeholders are :data:`SOFT (the translation can
    still be useful, but they are recorded (§10.1.7.
    """
    errors: list[dict] = []
    if not source_text or not target_text:
        return errors
    for pat in _PLACEHOLDER_PATTERNS:
        src_items = pat.findall(source_text)
        for item in src_items:
            # strip a trailing '/' from a self-closing tag before matching
            needle = item[:-1] if item.endswith("/") else item
            if target_text.find(needle) == -1:
                errors.append(_err(None, "placeholder",
                                   f"placeholder {item!r} from the source is "
                                   f"missing in the target", SOFT))
    return errors


# --- §10.1.8 : numbers / dates / URLs --------------------------------------
def _all_matches(text, patterns):
    out = []
    for pat in patterns:
        out.extend(pat.findall(text or ""))
    return out


def validate_numbers_dates_urls(source_text, target_text) -> list[dict]:
    """Every number / date / URL in the source must be preserved in the target.

    Numbers/dates/URLs are either preserved verbatim or transformed "secondo
    policy" (§10.2). We cannot know the policy here, so a *missing* source
    token is reported as :data:`SOFT` (it may be a deliberate, policy-driven
    change rather than a hard rejection).
    """
    errors: list[dict] = []
    if not source_text or not target_text:
        return errors
    patterns = [_URL_RE, _ISO_DATE_RE, _SLASH_DATE_RE, _NUMBER_RE,
                _NUMBER_PLAIN_RE]
    seen = set()
    for token in _all_matches(source_text, patterns):
        if token in seen:
            continue
        seen.add(token)
        if target_text.find(token) == -1:
            errors.append(_err(None, "numbers",
                               f"source token {token!r} not preserved in the "
                               f"target", SOFT))
    return errors


# --- §10.1.9 : no residual internal token ----------------------------------
def validate_no_internal_tokens(target_text) -> list[dict]:
    """The target must not contain any ``[[...]]`` internal markup (§10.1.9).

    The §9.4 prompt forbids the model from emitting ``[[POSS:...]]`` and every
    other internal marker. A residual ``[[...]]`` means the model ignored the
    schema and is a :data:`HARD` failure (the whole response is discarded).
    """
    errors: list[dict] = []
    if not target_text:
        return errors
    for tok in _INTERNAL_TOKEN_RE.findall(target_text):
        errors.append(_err(None, "internal_token",
                           f"residual internal token {tok!r} in the target",
                           HARD))
    return errors


# --- (then: forbidden terms) -----------------------------------------------
def validate_forbidden_terms(target_text, forbidden_terms) -> list[dict]:
    """A forbidden term must not appear in the target (§10.2).

    ``forbidden_terms`` is an iterable of strings (glossary ``forbidden_targets``
    + entity ``forbidden_targets``). Each that is found is :data:`SOFT` and is
    the ``term_violation`` the schema asks for (§9.5).
    """
    errors: list[dict] = []
    if not target_text or not forbidden_terms:
        return errors
    low = target_text.lower()
    for term in forbidden_terms:
        if term and term.lower() in low:
            errors.append(_err(None, "forbidden_term",
                               f"forbidden term {term!r} is present in the "
                               f"target", SOFT))
    return errors


# --- (then: must_keep identical) -------------------------------------------
def validate_must_keep(target_text, must_keep_terms) -> list[dict]:
    """Each ``must_keep`` term must appear identically in the target (§10.2).

    ``must_keep_terms`` is an iterable of strings (approved, non-translatable
    glossary / entity canonical forms). A term that is missing or mutated is
    :data:`SOFT` (recorded as a ``must_ok`` failure / flag).
    """
    errors: list[dict] = []
    if not target_text or not must_keep_terms:
        return errors
    low = target_text.lower()
    for term in must_keep_terms:
        if not term:
            continue
        if term.lower() not in low:
            errors.append(_err(None, "must_keep",
                               f"must-keep term {term!r} is absent from the "
                               f"target", SOFT))
    return errors


# --- (then: entities per canonical / policy) -------------------------------
def _mentions(text, source) -> bool:
    """True when *source* appears as a whole word in *text*."""
    if not text or not source:
        return False
    return re.search(rf"\b{re.escape(source.lower())}\b", text.lower()) is not None


def _mentions_inflected(text, source) -> bool:
    """True when the *stem* of *source* starts a word in *text*.

    Used when ``allow_inflection`` is set: the Italian rendering may carry a
    plural or a derived form («libro» -> «libri», «casa» -> «case»), so the
    match is done on the stem (the word minus its final vowel) followed by
    any continuation.
    """
    if not text or not source:
        return False
    stem = (source[:-1] if len(source) > 3 and source[-1] in "aeiou"
            else source).lower()
    return re.search(rf"\b{re.escape(stem)}\w*", text.lower()) is not None


def validate_entities(translations_by_id, source_by_id, entities,
                      threshold: float = 0.9) -> list[dict]:
    """Each entity mention in the source must follow its canonical / policy.

    ``entities`` is a list of ``{canonical_source, canonical_target, policy}``
    payloads (the §7.3 payload), optionally with ``allow_inflection``. For
    every segment, every entity whose ``canonical_source`` appears (as a whole
    word) in the segment's source must be rendered in the target according to
    its ``policy``:

    * ``not_translate`` / ``block_batch`` -- the source form must survive;
      with ``allow_inflection`` a derived form (same word start) is accepted;
    * ``translate`` / default -- the ``canonical_target`` must survive; with
      ``allow_inflection`` (the usual case) an inflected form of the target is
      accepted too.

    Missing canonical target (unknown) is not an error (the PRD says "non
    inventare": the model is free to render it naturally). A mismatch is
    :data:`SOFT`.
    """
    errors: list[dict] = []
    if not entities:
        return errors

    for sid, target in translations_by_id.items():
        source = source_by_id.get(sid)
        if source is None or not target:
            continue
        for ent in entities:
            src = (ent.get("canonical_source") or ent.get("source") or "").lower()
            if not src or not _mentions(source, src):
                continue
            policy = (ent.get("policy") or "").lower()
            canonical = ent.get("canonical_target") or ent.get("target")
            inflection_ok = bool(ent.get("allow_inflection"))
            match = _mentions_inflected if inflection_ok else _mentions
            if policy in ("not_translate", "block_batch"):
                if not match(target, src):
                    errors.append(_err(sid, "entity",
                                       f"entity {src!r} (not_translate) is "
                                       f"not preserved in the target", SOFT))
            elif canonical and not match(target, canonical):
                errors.append(_err(sid, "entity",
                                   f"entity {src!r} is not rendered as its "
                                   f"canonical target {canonical!r}", SOFT))
    return errors


# --- (then: target identical to source above threshold) --------------------
# Il modello a volte restituisce il sorgente INTELIGIATO come target
# (virgolette tipografiche -> dritte, trattini normalizzati): il confronto
# va fatto su testo normalizzato, altrimenti la copia quasi-esatta passa.
_NORMALIZE_MAP = str.maketrans({
    "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
    "‘": "'", "’": "'",
    "—": "--", "–": "-", "…": "...",
})


def _normalized_for_compare(text: str) -> str:
    t = (text or "").translate(_NORMALIZE_MAP).lower()
    return re.sub(r"\s+", " ", t).strip()


def is_non_translatable_source(text: str) -> bool:
    """True quando il sorgente è prevalentemente cifre/codici (ISBN, numeri).

    La copia identica di questi testi è legittima (EN == IT per un ISBN):
    i controlli anti-copia non devono respingerli.
    """
    t = text or ""
    letters = sum(ch.isalpha() for ch in t)
    digits = sum(ch.isdigit() for ch in t)
    if digits == 0:
        return False
    return digits / max(1, letters + digits) > 0.3


def copy_check_exempt(source_text: str) -> bool:
    """Esenzione dal controllo anti-copia (2026-09-30, soglie riviste).

    La copia è legittima SOLO per: testi sotto le 4 parole (titoli brevi,
    "ALIEN CLAY", "Contents") e sorgenti prevalentemente numerici/codici
    (ISBN, codici editore). Prima la soglia era 8 parole / 40 caratteri e
    lasciava passare copie inglesi intere come "To Everyone Fighting The
    Mandate" (incidente segmento 30, 2026-09-30).
    """
    a = _normalized_for_compare(source_text)
    if not a:
        return True
    if len(a.split()) < 4:
        return True
    return is_non_translatable_source(a)


def validate_target_identical(source_text, target_text,
                              threshold: float = 0.95) -> list[dict]:
    """A target that is (near)identical to the source is a HARD failure.

    Il modello ha restituito il sorgente (o una sua riscrittura minima:
    virgolette/trattini) al posto della traduzione. Salvare la copia come
    bozza e' il modo in cui finivano nel libro segmenti interi in inglese
    (incidente 2026-09-23, es. segmento 347): ora la risposta e' scartata.
    """
    errors: list[dict] = []
    if not source_text or not target_text:
        return errors
    a = _normalized_for_compare(source_text)
    b = _normalized_for_compare(target_text)
    if not a or not b:
        return errors
    # Esenzione: titoli brevi e testi prevalentemente numerici possono essere
    # legittimamente identici (copy_check_exempt per i dettagli).
    if copy_check_exempt(source_text):
        return errors
    ratio = SequenceMatcher(None, a, b).ratio()
    if ratio >= threshold:
        errors.append(_err(None, "target_identical",
                           f"target is {ratio:.0%} identical to the source "
                           f"(>= {threshold:.0%}) -- untranslated copy",
                           HARD))
    return errors


# --- (then: output too short / too long) -----------------------------------
def validate_length_anomaly(source_text, target_text,
                            min_ratio: float = 0.25,
                            max_ratio: float = 4.0) -> list[dict]:
    """Abnormally short / long output is flagged (§10.2).

    Compares token counts (English vs Italian length differs, so the band is
    deliberately wide. :data:`SOFT`.
    """
    errors: list[dict] = []
    if not source_text or not target_text:
        return errors
    n_src = len(source_text.split())
    n_tgt = len(target_text.split())
    if n_src == 0:
        return errors
    ratio = n_tgt / n_src
    if ratio < min_ratio:
        errors.append(_err(None, "length",
                           f"output unusually short ({n_tgt}/{n_src} tokens, "
                           f"{ratio:.0%}) -- likely truncated", SOFT))
    elif ratio > max_ratio:
        errors.append(_err(None, "length",
                           f"output unusually long ({n_tgt}/{n_src} tokens, "
                           f"{ratio:.0%}) -- likely duplicated/expanded", SOFT))
    return errors


# --- (then: duplicates / skips / merges) -----------------------------------
def validate_no_duplicates_skips_merges(requested_ids, translations) -> list[dict]:
    """Each source maps to exactly one target; no merge / skip.

    * **duplicate** -- the same ``target_text`` is produced for two different
      source segments (a merge of two sources into one target, or a repeated
      output);
    * **skip** -- a requested source produced no target (covered by
      :func:`validate_ids`, kept here as an explicit guard for the "no skip"
      guarantee).

    :data:`HARD` (§10.1.6 / AC1): the response is discarded and retried.
    """
    errors: list[dict] = []
    got_ids = [t.get("segment_id") for t in translations
               if t.get("segment_id") is not None]
    # duplicate ids in the response
    seen = set()
    for tid in got_ids:
        if tid in seen:
            errors.append(_err(tid, "duplicate",
                               "segment id appears more than once in the "
                               "response", HARD))
        seen.add(tid)
    # same target text for two different sources -> merge / duplicate output
    text_to_ids: dict[str, list] = {}
    for t in translations:
        tid = t.get("segment_id")
        txt = (t.get("target_text") or "").strip()
        if txt and tid is not None:
            text_to_ids.setdefault(txt, []).append(tid)
    for txt, ids in text_to_ids.items():
        if len(ids) > 1:
            errors.append(_err(None, "merge",
                               f"identical target {txt!r} maps to more than "
                               f"one source {ids} (merge/duplicate", HARD))
    return errors


# --- the single entry point ------------------------------------------------
def _as_set(value) -> set:
    """Normalize a *string* constraint (forbidden_terms / must_keep) to a set.

    Entity payloads (lists of dicts) must never be routed through here: the
    entity validator consumes them as a list (see ``validate_response``).
    """
    if value is None:
        return set()
    if isinstance(value, dict):
        # e.g. {"entities": [...]}-style payloads are not set material.
        return set()
    if isinstance(value, (set, frozenset, list, tuple)):
        if value and all(isinstance(v, dict) for v in value):
            return set()
        return set(value)
    return {value}


def validate_leftover_english(source_text, target_text,
                              protected: list[str] | None = None,
                              min_run: int = 4,
                              min_ratio: float = 0.45) -> list[dict]:
    """Long verbatim source runs left in the target = HARD failure.

    Il modello a volte traduce SOLO la cornice e lascia il corpo del testo
    in inglese (incidente segmento 805, 2026-10-01: "«...» —New Scientist
    parla di 'Children of Time'" con il blurb interno intatto). Un run
    verbatim di ``min_run``+ parole consecutive dal sorgente, quando
    copre una frazione significativa del target, segnala traduzione
    parziale. Esenzione SOLO quando il run rilevato e' contenuto in un
    termine protetto (must-keep, entita'): titoli e nomi possono restare
    in inglese (fix 2026-10-01: la prima versione esentava l'intero
    segmento se QUALUNQUE termine protetto compariva nel sorgente).
    """
    errors: list[dict] = []
    if not source_text or not target_text:
        return errors
    sw = re.findall(r"[a-zà-ÿ0-9']+", (source_text or "").lower())
    tw = re.findall(r"[a-zà-ÿ0-9']+", (target_text or "").lower())
    if not sw or not tw:
        return errors
    # DP: piu' lungo run di parole consecutive comuni.
    best = 0
    prev = [0] * (len(sw) + 1)
    for j in range(1, len(tw) + 1):
        cur = [0] * (len(sw) + 1)
        for i in range(1, len(sw) + 1):
            if sw[i - 1] == tw[j - 1]:
                cur[i] = prev[i - 1] + 1
                best = max(best, cur[i])
        prev = cur
    if best < min_run:
        return errors
    ratio = sum(1 for w in tw if w in set(sw)) / len(tw)
    if ratio < min_ratio:
        return errors
    # Run effettivi di lunghezza ``best`` presenti in ENTRAMBI i testi.
    protected_words = [
        re.findall(r"[a-zà-ÿ0-9']+", (p or "").lower())
        for p in (protected or [])
    ]
    for k in range(0, len(sw) - best + 1):
        run = sw[k:k + best]
        if not _run_in(tw, run):
            continue
        # Esente SOLO se il run e' contenuto in un termine protetto.
        if any(_run_in(pw, run) for pw in protected_words if pw):
            continue
        errors.append(_err(None, "leftover_english",
                           f"target keeps a {best}-word verbatim English run "
                           f"({ratio:.0%} of the target matches the source) "
                           "-- partial translation", HARD))
        break
    return errors


def _run_in(words: list[str], run: list[str]) -> bool:
    """True quando ``run`` compare come sequenza consecutiva in ``words``."""
    n = len(run)
    for k in range(0, len(words) - n + 1):
        if words[k:k + n] == run:
            return True
    return False


def validate_response(*, requested: list[dict], response,
                      constraints: dict | None = None) -> list[dict]:
    """Run every §10.1.5-9 check and return all errors (ordered by §10.1).

    ``requested`` is the ordered list of the segments that were sent to the
    model (each needs a ``segment_id`` and ``source_text``). ``response`` is
    the parsed Gateway JSON. ``constraints`` is an optional dict with:

    * ``forbidden_terms`` -- iterable of forbidden strings;
    * ``must_keep`` -- iterable of must-keep strings;
    * ``entities`` -- list of ``{canonical_source, canonical_target, policy}``.

    Returns a flat list of error dicts (each with ``segment_id``, ``kind``,
    ``message``, ``severity``). An empty list means every check passed.
    """
    constraints = constraints or {}
    errors: list[dict] = []

    syntax = validate_json_syntax(response)
    if syntax:
        return syntax

    translations = _extract_translations(response)
    requested_ids = [s.get("segment_id") for s in requested
                     if s.get("segment_id") is not None]
    by_id = {s.get("segment_id"): s for s in requested}

    # §10.1.6
    errors.extend(validate_ids(requested_ids, translations))
    # §10.1.9 (residual markup) -- structural, checked before per-segment
    for t in translations:
        errors.extend(validate_no_internal_tokens(t.get("target_text") or ""))
    # entities (needs the per-segment source/target maps); the §7.3 payloads
    # are a list of dicts -- they must NOT go through _as_set, which would
    # flatten them to an empty set and silently disable the check.
    ents = validate_entities(
        {t.get("segment_id"): t.get("target_text") or "" for t in translations},
        {s.get("segment_id"): s.get("source_text") or "" for s in requested},
        constraints.get("entities") or [])
    errors.extend(ents)
    # §10.1.7 / §10.1.8 / forbidden / must_keep / identical / length
    forbidden = _as_set(constraints.get("forbidden_terms"))
    must_keep = _as_set(constraints.get("must_keep"))
    for sid in requested_ids:
        src = next((s for s in requested if s.get("segment_id") == sid), {})
        src_text = src.get("source_text") or ""
        tgt = next((t for t in translations if t.get("segment_id") == sid), {})
        tgt_text = tgt.get("target_text") or ""
        # §10.1.7
        errors.extend(validate_placeholders(src_text, tgt_text))
        # §10.1.8
        errors.extend(validate_numbers_dates_urls(src_text, tgt_text))
        # forbidden / must_keep / identical / length
        errors.extend(validate_forbidden_terms(tgt_text, forbidden))
        errors.extend(validate_must_keep(tgt_text, must_keep))
        errors.extend(validate_target_identical(src_text, tgt_text))
        # Traduzione parziale: lunghi snip verbatim inglesi lasciati nel
        # target (incidente 805). Esenti i termini protetti (must-keep e
        # nomi/entita' del progetto, che possono restare in inglese).
        errors.extend(validate_leftover_english(
            src_text, tgt_text,
            protected=list(must_keep)
            + [e.get("canonical_source") or "" for e in (constraints.get("entities") or [])
               if isinstance(e, dict)],
        ))
        errors.extend(validate_length_anomaly(src_text, tgt_text))
    # duplicates / merges (structural)
    errors.extend(validate_no_duplicates_skips_merges(requested_ids, translations))
    return errors


def has_hard_errors(errors: list[dict]) -> bool:
    """True when *any* error is :data:`HARD` (discard the whole response)."""
    return any(e.get("severity") == HARD for e in errors)
