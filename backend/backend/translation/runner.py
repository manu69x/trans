"""Translation batch execution (PRD §10.1, §10.2, §8.4, §15.3 / task F3).

Job ``translate``: one block of CAT segments is sent to Gateway with a
JSON-schema / ``json_object``-constrained prompt (§9.5); the response is
validated against every deterministic control (§10.1.5-9 / §10.2); a *valid*
batch is written to ``translation_units`` as ``machine_draft`` (never
``approved``) and the run is recorded on ``llm_runs``; an *invalid* batch is
discarded and retried (§15.3 AC1).

The heavy steps mirror :mod:`backend.llm_ner_runner`:

 1. resolve the translation model (§8.2/§8.1: project setting or proxy
    auto-resolution, never hardcoded);
 2. build the block plan (§10.1 steps 1-3) from immutable snapshots;
 3. one constrained Gateway call for the whole block, rate-limited and
    idempotent per ``run_ref`` (§8.4 / §13.2);
 4. run the deterministic validators (§10.1.5-9); a hard failure rejects the
    whole response and the job fails (retryable, §15.3 AC1); a soft failure is
    recorded on the run and the segment is still saved as ``machine_draft``;
 5. save each segment idempotently (``TranslationUnit`` keyed by
    ``segment_id``): an already-``machine_draft`` segment is skipped, so a
    resume after a crash never duplicates a segment (§8.4 / AC3).

Prompts / outputs are stored as hashes / the raw response on the ``llm_runs``
row only; the job result and the audit log carry counts, IDs and hashes, never
manuscript text (§13.1).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.config import (
    FALLBACK_TRANSLATION_MODEL,
    LLM_GATEWAY_BASE_URL,
    LLM_GATEWAY_MAX_OUTPUT_TOKENS,
    assert_local_url,
)
from backend.gateway_http import (
    GatewayClient,
    GatewayInvalidJSON,
    GatewayUnavailable,
    sha256_hex,
)
from backend.models import (
    AuditLog,
    Entity,
    GlossaryTerm,
    Job,
    LLMRun,
    Project,
    TranslationMemoryEntry,
    TranslationUnit,
)
from .planner import TokenBudget, build_block_plan
from backend.parsing.chunking import MAX_BLOCK_TOTAL_TOKENS
from ..segment_numbering import next_numero_start
from .tm_retrieval import is_incompatible_context, retrieve_tm
from .validators import (
    SOFT,
    has_hard_errors,
    validate_response,
)

RUN_TYPE = "translate"
STATUS_DRAFT = "machine_draft"
SCHEMA = (
    "{\n"
    '  "translations": [\n'
    '    {"segment_id": "uuid", "target_text": "...",'
    ' "used_entity_ids": [], "term_violations": [],'
    ' "flags": []}\n'
    "  ]\n"
    "}"
)


def _is_text_generation_model(model_id: str, name: str) -> bool:
    """Heuristic proxy-driven role filter (§8.1/§8.2: no hardcoded models).

    llama-swap exposes every routed backend in one list — TTS, embeddings,
    whisper, rerankers included. The translation model must be a text LLM, so
    auto-resolution skips the rows whose id/name marks another role. The filter
    reads only what the proxy publishes (id + display name); no model name is
    ever hardcoded here.
    """
    haystack = f"{model_id} {name}".lower()
    return not re.search(
        r"\b(tts|whisper|embed|embedding|rerank|clip)\b", haystack
    )


def _resolve_model(db: Session, project: Project, client: GatewayClient) -> str:
    """The translation model (§8.2/§8.1): project setting or proxy auto-resolve.

    No hardcoded names (§8.1): when the project has no ``translation_model_id``
    the proxy's own list decides -- every non-text backend is skipped
    (:func:`_is_text_generation_model`) and a currently ``loaded`` text model is
    preferred (llama-swap loads on demand, but an already-loaded model is the
    one that can actually serve the batch right now). The choice is recorded on
    the project (reproducible run, §8.5).
    """
    if project.translation_model_id:
        return project.translation_model_id

    models = client.list_models()
    if not models:
        raise GatewayUnavailable("Gateway exposes no models (§8.1)")
    text_models = [
        m for m in models
        if _is_text_generation_model(m["id"], m.get("display_name") or "")
    ]
    pool = text_models or models
    loaded = [m for m in pool if m.get("status") == "loaded"]
    chosen = (loaded or pool)[0]
    project.translation_model_id = chosen["id"]
    db.add(project)
    return chosen["id"]


def _cached_run(db: Session, project_id: str, run_ref: str) -> LLMRun | None:
    """A completed ``translate`` run keyed by ``run_ref`` (§8.4 / §13.2).

    Re-running the *same* batch (same ``run_ref``) reuses the stored response
    instead of calling Gateway again -- a warm run performs zero new LLM calls.
    """
    return (
        db.query(LLMRun)
        .filter(LLMRun.project_id == project_id,
                LLMRun.run_type == RUN_TYPE,
                LLMRun.run_ref == run_ref,
                LLMRun.status == "completed")
        .order_by(LLMRun.created_at.desc())
        # first(), non one_or_none(): due tentativi ravvicinati possono avere
        # creato due run completati con lo stesso run_ref (corsa §8.4) e
        # one_or_none sollevava MultipleResultsFound, bloccando per sempre
        # ogni retry del blocco (bug 2026-09-19).
        .first()
    )


def _build_run_ref(project_id: str, segments: list[dict], model: str,
                   temperature: float, seed, block_source: str,
                   previous_context: str) -> str:
    """Deterministic ``run_ref`` for a batch (idempotency key, §8.4).

    A re-run of the same block with the same configuration hashes to the same
    ``run_ref`` and therefore resumes from the stored response.
    """
    ids = "|".join(sorted(str(s.get("segment_id")) for s in segments
                          if s.get("segment_id")) or ["none"])
    blob = (f"{project_id}|{RUN_TYPE}|{model}|{temperature}|{seed}|{block_source}|"
            f"{previous_context}|{ids}")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _constraints_for(project_id: str, db: Session) -> dict:
    """The approved glossary / entity constraints for the project (§10.2).

    * ``forbidden_terms`` -- every forbidden target (glossary + entity);
    * ``must_keep`` -- every approved, non-translatable canonical form;
    * ``entities`` -- the §7.3 payloads used by the entity validator.
    """
    forbidden: set[str] = set()
    must_keep: set[str] = set()
    entities: list[dict] = []

    for t in db.query(GlossaryTerm).filter(
        GlossaryTerm.project_id == project_id,
        GlossaryTerm.status == "approved",
    ).all():
        for fb in (t.forbidden_targets or []):
            if fb:
                forbidden.add(fb)
        # an approved, non-translatable term must survive verbatim
        if t.preferred and t.target_term:
            must_keep.add(t.target_term)
        entities.append({
            "canonical_source": t.source_term,
            "canonical_target": t.target_term,
            "policy": "not_translate" if t.preferred else "translate",
        })

    for e in db.query(Entity).filter(
        Entity.project_id == project_id,
        # §10.2: i vincoli di traduzione nascono SOLO da entità approvate —
        # le proposte non confermate dall'umano non vincolano il modello né
        # la validazione (decisione 2026-09-19).
        Entity.status == "approved",
    ).all():
        for fb in (e.forbidden_targets or []):
            if fb:
                forbidden.add(fb)
        policy = "not_translate" if e.never_translate else "translate"
        entities.append({
            "canonical_source": e.canonical_source,
            "canonical_target": e.canonical_target,
            "policy": policy,
            "allow_inflection": bool(e.allow_inflection),
        })

    return {"forbidden_terms": forbidden, "must_keep": must_keep,
            "entities": entities}


def _split_prompt(prompt: str) -> tuple[str, str]:
    """Split the assembled §9.4 prompt into (system, user).

    Everything before the ``TESTO DA TRADURRE`` marker is the system prompt
    (role, priorities, genre, glossary, TM, context); from the marker on is
    the user prompt (the text to translate + the output/schema instructions).
    """
    marker = "TESTO DA TRADURRE"
    idx = prompt.find(marker)
    if idx == -1:
        return prompt, ""
    return prompt[:idx].strip(), prompt[idx:].strip()


def _save_segments(db: Session, project_id: str, chapter_id, segments,
                   translations: list[dict], model_run_id: str) -> int:
    """Idempotently persist each validated segment as ``machine_draft``.

    ``TranslationUnit`` is keyed by ``segment_id`` (the primary key), so a
    segment that is already ``machine_draft`` is skipped and never duplicated
    (§8.4 / AC3: a resume after a crash does not double-write a segment).
    Approved segments are never touched (§5.1).
    """
    saved = 0
    by_id = {s.get("segment_id"): s for s in segments
             if s.get("segment_id") is not None}
    next_n = next_numero_start(db, project_id)
    # keys MUST be strings: payload segment_ids are strings while u.id comes
    # back from psycopg2 as a UUID object -- a mixed-key dict silently misses
    # the lookup and the INSERT below would violate translation_units_pkey.
    existing = {str(u.id): u for u in (
        db.query(TranslationUnit)
        .filter(TranslationUnit.project_id == project_id).all())}
    for t in translations:
        sid = t.get("segment_id")
        target = t.get("target_text")
        if not sid or target is None:
            continue
        row = existing.get(sid)
        if row is not None and row.status == "approved":
            continue  # §5.1: le versions approvate non si toccano
        src = by_id.get(sid, {})
        if row is None:
            row = TranslationUnit(
                id=sid,
                project_id=project_id,
                chapter_id=chapter_id,
                ordinal=src.get("ordinal"),
                source_text=src.get("source_text") or "",
                source_hash=src.get("source_hash") or "",
                source_flags=src.get("source_flags") or {},
            )
            existing[sid] = row
        elif _normalize_for_compare(row.target_text or "") != \
                _normalize_for_compare(target):
            # La traduzione e' cambiata: i punteggi QE si riferivano alla
            # vecchia bozza, non sono piu' validi (si ricalcola con
            # "Verifica QE").
            row.is_italian = None
            row.is_translated = None
            row.is_english = None
        row.target_text = target
        row.status = STATUS_DRAFT
        row.model_run_id = model_run_id
        db.add(row)
        saved += 1
    return saved


def _count_errors(errors: list[dict]) -> dict:
    out: dict = {}
    for e in errors:
        out[e["kind"]] = out.get(e["kind"], 0) + 1
    return out


def _fail_run(db: Session, job: Job, project: Project, model: str,
              prompt_hash: str, run_ref: str, error: str) -> None:
    """Record a failed / rejected run and fail the job (retryable)."""
    db.add(LLMRun(
        project_id=str(project.id),
        run_type=RUN_TYPE,
        model_name=model,
        prompt_hash=prompt_hash or "",
        parameters={"run_ref": run_ref, "error": error},
        status="failed",
        error=error[:500],
        run_ref=run_ref,
        completed_at=datetime.utcnow(),
    ))
    db.commit()
    job.status = "failed"
    job.error = error[:500]
    job.completed_at = datetime.utcnow()
    db.add(job)
    db.commit()


def _normalize_model_response(response):
    """Tolerate small-model schema deviations (§9.5, same spirit as
    ``_parse_model_json``): the wire contract is
    ``{"translations": [{"segment_id", "target_text", ...}]}``, but local
    models sometimes return a bare list or name the target field
    ``translation``/``translated_text``/``text``. Canonicalise here so the
    deterministic validators keep enforcing the real §10.1 rules.
    """
    if isinstance(response, list):
        response = {"translations": response}
    if not isinstance(response, dict):
        return response
    items = response.get("translations")
    if not isinstance(items, list):
        return response
    fixed = False
    out = []
    for it in items:
        if (isinstance(it, dict) and not it.get("target_text")
                and it.get("segment_id") is not None):
            for alt in ("translation", "translated_text", "target", "text"):
                if it.get(alt):
                    it = {**it, "target_text": it[alt]}
                    fixed = True
                    break
        out.append(it)
    return {**response, "translations": out} if fixed else response


def _dedupe_translations(resp):
    """Riparazione anti-duplicato: un id piu' volte nel JSON -> resta il
    target PIU' LUNGO (il modello a volte emette prima una versione parziale
    e poi quella completa; incidente segmento 724)."""
    trs = (resp or {}).get("translations") or []
    if not trs:
        return resp
    best: dict[str, dict] = {}
    order: list[str] = []
    for t in trs:
        if not isinstance(t, dict):
            continue
        sid = t.get("segment_id")
        if sid is None:
            continue
        if sid not in best:
            best[sid] = t
            order.append(sid)
        else:
            cur = best[sid].get("target_text") or ""
            new = t.get("target_text") or ""
            if len(new) > len(cur):
                best[sid] = t
    return {"translations": [best[sid] for sid in order]}


def _normalize_for_compare(text: str) -> str:
    """Normalizza virgolette/trattini/spazi per il confronto anti-copia."""
    t = (text or "").translate(str.maketrans({
        "“": '"', "”": '"', "„": '"', "«": '"', "»": '"',
        "‘": "'", "’": "'",
        "—": "--", "–": "-", "…": "...",
    })).lower()
    return " ".join(t.split())


# Prefiltro economico anti-inglese: conta i marcatori di italiano. Una
# traduzione italiana ha quasi sempre >= 2 di queste parole o un accento.
# NB: il pronome italiano "i" va cercato case-sensitive, altrimenti fa
# matchare la pronome inglese "I" e fa passare i paragrafi inglesi per
# italiani (bug 2026-09-24, segmento 45).
_IT_MARKER_RE = re.compile(
    r"\b(che|non|per|con|una|uno|il|la|le|gli|del|della|dello|degli|"
    r"delle|questo|questa|quello|quella|sono|essere|stato|stata|stati|"
    r"state|aveva|avevo|avevano|nella|nello|nelli|alla|dalla|sulla|"
    r"piu|più|gia|già|cosi|così|anche|loro|noi|voi|ma|come|dove|quando|"
    r"se|si|ha|hanno|ho|hai|sua|suo|miei|tuoi|suoi|questi|queste|"
    r"quelli|quelle|molto|troppo|poi|invece|davvero|forse)\b",
    re.IGNORECASE,
)
_IT_I_RE = re.compile(r"\bi\b")
_ACCENT_RE = re.compile(r"[àèéìíòóù]")


def _looks_english(text: str) -> bool:
    """True se il testo non mostra marcatori di italiano."""
    t = text or ""
    if _ACCENT_RE.search(t):
        return False
    score = len(_IT_MARKER_RE.findall(t)) + len(_IT_I_RE.findall(t))
    return score < 2


def _response_is_english(response, segments: list[dict]) -> bool:
    """True quando qualche target e' inglese non tradotto.

    Il confronto normalizzato non basta: il modello a volte RISCRIVE il
    testo inglese con parole diverse (incidente segmenti 45/70). Su ogni
    target senza marcatori italiani chiede conferma al rilevatore Spark
    (is_english >= 0.5). Se il rilevatore non risponde non blocca.
    """
    translations = (response or {}).get("translations") or []
    src_by_id = {s.get("segment_id"): s.get("source_text") or ""
                 for s in segments}
    for t in translations:
        tgt = t.get("target_text") or ""
        if len(tgt) < 40 or not _looks_english(tgt):
            continue
        src = src_by_id.get(t.get("segment_id")) or ""
        try:
            from backend.qe_client import verify_segment_qe
            if verify_segment_qe(src, tgt)["is_english"] >= 0.5:
                return True  # confermato inglese dal rilevatore
            # Spark dice NON inglese: il sospetto dell'euristica e' sciolto
            # (fix 2026-10-01: prima il verdetto di assoluzione veniva
            # ignorato e la funzione ritornava comunque suspicious=True,
            # scartando traduzioni corrette con titoli/nomi inglesi dentro,
            # es. i blurb "—New Scientist on Children of Time").
        except Exception:  # noqa: BLE001 - il check non deve bloccare
            return False
    return False


def _is_source_copy(response, segments: list[dict]) -> bool:
    """True quando qualche target restituito e' il sorgente non tradotto.

    Il modello a volte emette il testo inglese come traduzione (anche
    ri-serializzando virgolette e trattini): confronto normalizzato.
    """
    translations = (response or {}).get("translations") or []
    src_by_id = {s.get("segment_id"): s.get("source_text") or ""
                 for s in segments}
    from .validators import copy_check_exempt

    for t in translations:
        src = src_by_id.get(t.get("segment_id"))
        tgt = t.get("target_text") or ""
        # Il target e' il sorgente copiato: respinto SOLO se il sorgente
        # non e' esente (titoli <4 parole e testi numerici sono copia
        # legittima; prima la soglia len>40 lasciava passare frasi inglesi
        # intere come il segmento 30, incidente 2026-09-30).
        if src and tgt and not copy_check_exempt(src) \
                and _normalize_for_compare(tgt) == _normalize_for_compare(src):
            return True
    return False


def _finish_resilient(db: Session, job: Job, project: Project, response,
                      run_ref: str, branch, model: str, parameters: dict, *,
                      resume: bool, segments: list[dict]) -> None:
    """_finish con recupero: se la connessione DB e' caduta durante la
    chiamata LLM (flip di rete WSL/mirrored), ripete il salvataggio su una
    sessione fresca invece di perdere la traduzione completata."""
    attempt = 0
    while True:
        try:
            _finish(db, job, project, response, run_ref, branch, model,
                    parameters, resume=resume, segments=segments)
            return
        except OperationalError:
            attempt += 1
            if attempt >= 2:
                raise
            # la connessione e' probabilmente morta: se ne apre una nuova
            db.rollback()
            db.close()
            time.sleep(2)
            db = SessionLocal()


def _finish(db: Session, job: Job, project: Project, response, run_ref: str,
            branch, model: str, parameters: dict, resume: bool,
            segments: list[dict]) -> None:
    """Validate *response* and, if valid, persist it as ``machine_draft``.

    Hard validation failures reject the whole response (job fails, retryable);
    soft failures are recorded on the run and each segment is still saved as a
    ``machine_draft``.
    """
    project_id = project.id
    response = _normalize_model_response(response)
    errors = validate_response(requested=segments, response=response,
                               constraints=_constraints_for(project_id, db))
    if has_hard_errors(errors):
        kinds = "; ".join(
            f"{e['kind']}" for e in errors if e["severity"] == "hard")
        _fail_run(db, job, project, parameters.get("model", ""),
                  parameters.get("prompt_hash", ""), run_ref,
                  "response failed deterministic validation: " + kinds)
        return

    # valid: record the run and save every segment in ONE transaction
    output_hash = sha256_hex(json.dumps(response, ensure_ascii=False,
                                        sort_keys=True))
    run = LLMRun(
        project_id=str(project_id),
        run_type=RUN_TYPE,
        model_name=parameters.get("model", ""),
        prompt_hash=parameters.get("prompt_hash", ""),
        parameters={**parameters, "response": response,
                    "validation": {
                        "soft_errors": [e for e in errors
                                        if e["severity"] == "soft"],
                        "counts": _count_errors(errors),
                    },
                    "run_ref": run_ref,
                    "branch": parameters.get("branch")},
        output_hash=output_hash,
        status="completed",
        run_ref=run_ref,
        branch=parameters.get("branch"),
        completed_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()

    translations = (response.get("translations")
                    if isinstance(response, dict) else [])
    saved = _save_segments(
        db, str(project_id), (segments[0] or {}).get("chapter_id"),
        segments, translations, model_run_id=str(run.id))

    result = {
        "run_id": str(run.id),
        "run_ref": run_ref,
        "model": parameters.get("model", ""),
        "segments_saved": saved,
        "total_segments": len(segments),
        "hard_errors": [e for e in errors if e["severity"] == "hard"],
        "soft_errors": [e for e in errors if e["severity"] == "soft"],
        "resumed": resume,
        "output_hash": output_hash,
    }
    job.result = {**(job.result or {}), "translate": result}
    db.add(job)
    db.add(AuditLog(
        project_id=str(project_id),
        action="translation_run",
        entity="translation_block",
        entity_id=str(run.id),
        after={"run_ref": run_ref, "saved": saved,
               "soft_errors": len(result["soft_errors"]),
               "status": STATUS_DRAFT},
        created_at=datetime.utcnow(),
    ))
    db.commit()


def run_translation_batch(db: Session, job: Job) -> None:
    """Execute one translation block (§10.1 / §8.4 / §15.3).

    Payload keys (all optional except ``project_id`` and ``segments``):
    ``project_id``, ``segments`` (each ``segment_id`` + ``source_text`` and
    optionally ``chapter_id``/``ordinal``/``source_hash``/``source_flags``),
    ``block_source``, ``previous_context``, ``model`` (override),
    ``temperature``, ``seed``, ``idempotency_key`` / ``run_ref`` (idempotency),
    ``branch`` (comparable replay, §8.4), ``snapshot_ids``, ``max_output_tokens``,
    ``style_guide``, ``max_total_tokens`` and the TM retrieval thresholds.
    """
    project_id = job.payload["project_id"]
    segments = job.payload.get("segments") or []
    if not segments:
        raise ValueError("translate job payload has no segments (§5.4)")

    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")

    # §13.1 gate: fail before any payload can leave the machine.
    assert_local_url(LLM_GATEWAY_BASE_URL)

    run_ref = (job.payload.get("idempotency_key")
               or job.payload.get("run_ref")
               or _build_run_ref(
                   project_id, segments,
                   job.payload.get("model") or project.translation_model_id or "",
                   float(job.payload.get("temperature") or 0.0),
                   job.payload.get("seed"),
                   job.payload.get("block_source") or "",
                   job.payload.get("previous_context") or "",
               ))
    branch = job.payload.get("branch")

    # --- resume from a completed run with the same run_ref (§8.4) ----------
    cached = _cached_run(db, project_id, run_ref)
    if cached is not None and cached.parameters and cached.parameters.get("response"):
        response = cached.parameters["response"]
        return _finish_resilient(db, job, project, response, run_ref, branch,
                                 cached.model_name, cached.parameters,
                                 resume=True, segments=segments)

    # Chiusura della transazione di lettura anche PRIMA della risoluzione
    # modello: GatewayClient() e la risoluzione via proxy fanno I/O di rete
    # e la fase puo' superare i 5 min di idle_in_transaction_session_timeout
    # (stesso incidente del commit sotto, osservato live su pg_stat_activity).
    db.commit()

    # --- resolve the model (§8.1/§8.2) ------------------------------------
    client = GatewayClient()
    model = job.payload.get("model") or _resolve_model(db, project, client)

    # --- build the block plan (§10.1 steps 1-3) ---------------------------
    block_source = job.payload.get("block_source") or " ".join(
        (s.get("source_text") or "") for s in segments)
    constraints = _constraints_for(project_id, db)

    # TM retrieval (§7.2, project-scoped + threshold); drop incompatible hits.
    tm_candidates = [
        {
            "id": str(r.id),
            "project_id": str(r.project_id),
            "chapter_id": str(r.chapter_id) if r.chapter_id else None,
            "source_normalized": r.source_normalized,
            "source_original": r.source_original,
            "target_approved": r.target_approved,
            "genre_profile": r.genre_profile,
            "pov": r.pov,
            "score": None,
            "method": None,
            "project_ok": True,
            "source_embedding": ([float(x) for x in r.source_embedding]
                                 if r.source_embedding is not None else None),
        }
        for r in db.query(TranslationMemoryEntry).filter(
            TranslationMemoryEntry.project_id == project_id).all()
    ]
    matches = retrieve_tm(
        block_source=block_source,
        candidates=tm_candidates,
        project_id=project_id,
        exact_threshold=float(job.payload.get("exact_threshold") or 1.0),
        fuzzy_threshold=float(job.payload.get("fuzzy_threshold") or 0.5),
        semantic_threshold=float(job.payload.get("semantic_threshold") or 0.6),
        max_matches=int(job.payload.get("max_matches") or 5),
    )
    usable_tm = [m for m in matches
                 if not is_incompatible_context(
                     m, block_genre=project.genre_profile, block_pov=None)]

    budget = TokenBudget(max_total=int(job.payload.get("max_total_tokens")
                                       or MAX_BLOCK_TOTAL_TOKENS))
    # §9.4: the project's genre profile must reach the {{genre_style_prompt}}
    # section of the assembled prompt (not just the TM incompatibility filter).
    genre_profile = job.payload.get("genre_profile") or project.genre_profile
    # §7.3 selection: only entities mentioned in the block (or the two
    # preceding segments / coreference) enter the prompt. Injecting ALL
    # project entities blows the §5.4 token budget on real books
    # measured: 2.965 entities = ~47k tokens vs 16.384 ceiling).
    from ..glossary import select_entities_for_block

    # §7.3: solo le entità APPROVATE possono entrare nel prompt (le proposte
    # non confermate dall'umano non vincolano — decisione 2026-09-19).
    _all_entities = db.query(Entity).filter(
        Entity.project_id == project_id,
        Entity.status == "approved").all()
    _entity_payloads = [{
        "id": str(e.id),
        "canonical_source": e.canonical_source,
        "canonical_target": e.canonical_target,
        "aliases": [a.source_alias for a in (e.aliases or [])],
        "policy": "not_translate" if e.never_translate else "translate",
    } for e in _all_entities]
    _selection = select_entities_for_block(
        block_text=block_source,
        preceding_texts=[],
        coref_ids=(),
        entities=_entity_payloads,
        terms=[],
    )
    _selected_sources = {e["source"]
                         for e in _selection.get("entities", [])}
    entities_selected = [{
        "source": e.canonical_source,
        "target": e.canonical_target,
        "forbidden_targets": e.forbidden_targets or [],
        "policy": "not_translate" if e.never_translate else "translate",
        "gender": e.italian_grammatical_gender,
        "number": e.grammatical_number,
        "allow_inflection": bool(e.allow_inflection),
    } for e in _all_entities if e.canonical_source in _selected_sources]

    plan = build_block_plan(
        model_id=model,
        block_source=block_source,
        segments=[{"segment_id": s.get("segment_id"),
                   "source_text": s.get("source_text") or ""}
                  for s in segments],
        previous_context=job.payload.get("previous_context") or "",
        budget=budget,
        genre_profile=genre_profile,
        tm_matches=usable_tm,
        glossary_entries=[{"source": t.source_term, "target": t.target_term,
                            "forbidden_targets": t.forbidden_targets or [],
                            "policy": "not_translate" if t.preferred
                            else "translate",
                            "gender": t.grammatical_gender_it,
                            "number": t.grammatical_number}
                          for t in db.query(GlossaryTerm).filter(
                              GlossaryTerm.project_id == project_id,
                              GlossaryTerm.status == "approved").all()],
        entities=entities_selected,
        style_guide=job.payload.get("style_guide"),
        snapshot_ids=job.payload.get("snapshot_ids") or {},
        output_schema=SCHEMA,
    )

    if not plan.budget.get("within_ceiling", True):
        # §15.3: a block whose input + reserved output do not fit the model
        # context must not be started.
        _fail_run(db, job, project, model, plan.prompt_hash, run_ref,
                  "block does not fit the model context window (§15.3)")
        return

    system_prompt, user_prompt = _split_prompt(plan.prompt)
    max_tokens = int(job.payload.get("max_output_tokens")
                     or LLM_GATEWAY_MAX_OUTPUT_TOKENS)

    # La chiamata LLM dura minuti: chiudiamo qui la transazione di lettura,
    # altrimenti resta "idle in transaction" e Postgres la uccide per
    # idle_in_transaction_session_timeout (5 min) mentre attende il modello,
    # facendo fallire il salvataggio del blocco (§10.2, incidente 2026-09-23).
    db.commit()

    # --- anti-copia / anti-inglese: scala di escalation -------------------
    # (2026-09-23/24, incidenti segmenti 347/45/70/307/351/724) I guai del
    # modello primario sono di quattro tipi: JSON rotto, sorgente copiato
    # identico, inglese riscritto, output strutturalmente sbagliato
    # (cardinalita'/id duplicati). Ogni tentativo della scala viene
    # giudicato con TUTTI i controlli deterministici + il rilevatore
    # inglese: il primo tentativo pulito vince; se nessuno passa il job
    # muore con l'errore dell'ultimo tentativo (niente salvataggi sbagliati).
    def _response_untranslated(resp) -> bool:
        if _is_source_copy(resp, segments):
            return True
        return _response_is_english(resp, segments)

    T_payload = float(job.payload.get("temperature") or 0.0)
    # Scala: determinismo prima (stessa T), poi diversita' di campionamento
    # (T 0.2): i duplicati/merge deterministici (segmento 724) si rompono
    # solo variando il campionamento.
    cands = [(model, T_payload)]
    if FALLBACK_TRANSLATION_MODEL and FALLBACK_TRANSLATION_MODEL != model:
        cands.append((FALLBACK_TRANSLATION_MODEL, T_payload))
        if abs(T_payload) > 1e-9:
            cands.append((FALLBACK_TRANSLATION_MODEL, 0.0))
    cands.append((model, 0.2))
    if FALLBACK_TRANSLATION_MODEL and FALLBACK_TRANSLATION_MODEL != model:
        cands.append((FALLBACK_TRANSLATION_MODEL, 0.2))
    tentativi: list[tuple[str, float]] = []
    _visti: set[tuple[str, float]] = set()
    for m, t in cands:
        key = (m, round(t, 3))
        if key not in _visti:
            _visti.add(key)
            tentativi.append((m, t))

    response = None
    soft_errors: list[dict] = []
    last_error = "no model produced a valid response"
    constraints = _constraints_for(project_id, db)
    for attempt_model, attempt_temp in tentativi:
        try:
            resp = client.chat_json(
                model=attempt_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=attempt_temp,
                max_tokens=max_tokens,
                seed=job.payload.get("seed"),
            )
        except (GatewayInvalidJSON, GatewayUnavailable) as exc:
            last_error = str(exc)[:500]
            continue
        if _response_untranslated(resp):
            last_error = "translation is English/untranslated (anti-copy)"
            continue
        errors = validate_response(requested=segments, response=resp,
                                   constraints=constraints)
        hard = [e for e in errors if e["severity"] == "hard"]
        if hard and any(e["kind"] in ("duplicate", "cardinality", "merge")
                        for e in hard):
            # Riparazione anti-duplicato: id ripetuti -> target piu' lungo
            resp2 = _dedupe_translations(resp)
            if not _response_untranslated(resp2):
                errors2 = validate_response(requested=segments,
                                            response=resp2,
                                            constraints=constraints)
                if not has_hard_errors(errors2):
                    response = resp2
                    model = attempt_model
                    soft_errors = [e for e in errors2
                                   if e["severity"] == "soft"]
                    break
            last_error = ("response failed deterministic validation: "
                          + "; ".join(e["kind"] for e in hard))
            continue
        if hard:
            last_error = ("response failed deterministic validation: "
                          + "; ".join(e["kind"] for e in hard))
            continue
        response = resp
        model = attempt_model
        soft_errors = [e for e in errors if e["severity"] == "soft"]
        break

    if response is None:
        _fail_run(db, job, project, model, plan.prompt_hash, run_ref,
                  last_error)
        return

    _finish_resilient(db, job, project, response, run_ref, branch, model,
                      {"prompt_hash": plan.prompt_hash, "budget": plan.budget,
                       "model": model, "temperature": job.payload.get("temperature"),
                       "seed": job.payload.get("seed"),
                       "max_output_tokens": max_tokens,
                       "block_source": block_source, "context": system_prompt},
                      resume=False, segments=segments)
