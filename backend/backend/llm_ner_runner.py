"""LLM-structured NER runner (PRD §6.2.3, §8.2, §9.5) — task t_e87a4040.

Job ``llm_classify_entities``: classifies the high-ambiguity / domain
candidates left by the BookNLP+NER extraction through the analysis model
on LLM Gateway (§8.2: never hardcoded — resolved from the project's
``text_model_id`` or the first model the proxy lists).

Pipeline (all heavy steps mirror :mod:`backend.nlp_runner`):
 1. rebuild the scope text (chapter or whole project) exactly like the F2
    extraction — same confirmed-header strip, so quotes line up;
 2. select §6.2.3 candidates (pure layer :mod:`backend.parsing.ner_llm`);
 3. one **structured** Gateway call per block (JSON-constrained, temp
    0–0.2, reasoning off), rate-limited and idempotent per block hash:
    an ``llm_runs`` row keyed ``llm:<block_hash>`` caches the classification
    of the same candidates over the same text (re-runs cost zero calls);
 4. merge field-level into the proposed entities — **no overwrite** of
    BookNLP attributions (§6.2.3), the user's fields always win (§15.2);
 5. append one evidence row per LLM proposal with ``extractor='llm'`` and
    the model + confidence recorded (AC2 provenienza); prompts/outputs are
    stored as hashes only (§8.5).

Never touches non-proposed entities. Every counter lands in ``job.result``
for the benchmark (docs/benchmarks).
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy.orm import Session

from .config import (
    LLM_GATEWAY_BASE_URL,
    LLM_GATEWAY_MAX_OUTPUT_TOKENS,
    assert_local_url,
)
from .gateway_http import (
    GatewayClient,
    GatewayInvalidJSON,
    GatewayUnavailable,
    sha256_hex,
)
from .models import (
    AuditLog,
    Document,
    DocumentPage,
    Entity,
    EntityEvidence,
    ImportReport,
    Job,
    LLMRun,
    Project,
    StructureNode,
)
from .parsing import ner_llm
from .parsing import structure


def _scope_pages(db: Session, project_id: str, node_id: str | None) -> str:
    """The chapter/project text with confirmed headers stripped (§5.3).

    Same rule as :mod:`backend.nlp_runner` (its ``_scope_pages`` is
    tuple-shaped for BookNLP offset maps; here the joined text is enough
    for quote contexts, so this is the lean variant).
    """
    documents = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .order_by(Document.created_at)
        .all()
    )
    payloads: dict[int, dict] = {}
    for document in documents:
        rows = (
            db.query(DocumentPage)
            .filter(DocumentPage.document_id == document.id)
            .order_by(DocumentPage.page_number)
            .all()
        )
        for row in rows:
            payloads[int(row.page_number)] = dict(row.page_payload or {})

    if node_id is None:
        nodes = (
            db.query(StructureNode)
            .filter(StructureNode.project_id == project_id,
                    StructureNode.kind == "chapter")
            .order_by(StructureNode.start_page)
            .all()
        )
    else:
        node = db.get(StructureNode, node_id)
        if node is None:
            raise ValueError("unknown node_id in job payload")
        nodes = [node]

    # confirmed header/footer keys (§5.3), same rule as nlp_runner
    repeated: list[dict] = []
    if documents:
        report = (
            db.query(ImportReport)
            .filter(ImportReport.document_id == documents[0].id)
            .one_or_none()
        )
        repeated = list((report.summary or {})
                        .get("repeated_headers_footers") or [])
    if not repeated:
        counts: dict[str, int] = {}
        for payload in payloads.values():
            for ln in structure.page_lines(payload):
                key = structure.repeated_line_key(ln.get("text") or "")
                if key:
                    counts[key] = counts.get(key, 0) + 1
        repeated = [{"text": k, "pages": v} for k, v in counts.items()]
    confirmed = structure.confirm_repeated_lines(
        repeated, len(payloads) or 1)
    confirmed_keys = {c["text"] for c in confirmed}

    def _in_scope(page_number: int) -> bool:
        if node_id is not None:
            return any(
                int(n.start_page or 0) <= page_number <= int(n.end_page or 0)
                for n in nodes
            )
        return True  # whole project: every page in scope

    parts: list[str] = []
    for page_number in sorted(payloads):
        if not _in_scope(page_number):
            continue
        lines = [
            ln for ln in structure.page_lines(payloads[page_number])
            if structure.repeated_line_key(ln.get("text") or "")
            not in confirmed_keys
        ]
        text = "\n".join(ln["text"] for ln in lines).strip()
        if text:
            parts.append(text)
    if not parts:
        raise ValueError(
            "no text in scope: run import + extraction first (§5.2, §6.2)")
    return "\n\n".join(parts)


def _is_text_generation_model(model_id: str, name: str) -> bool:
    """Heuristic proxy-driven role filter (§8.1/§8.2: no hardcoded models).

    llama-swap exposes every routed backend in one list — TTS, embeddings,
    whisper, rerankers included. The analysis model must be a text LLM, so
    auto-resolution skips the rows whose id/name marks another role. The
    filter reads only what the proxy publishes (id + display name); no
    model name is ever hardcoded here.
    """
    haystack = f"{model_id} {name}".lower()
    return not re.search(
        r"\b(tts|whisper|embed|embedding|rerank|clip)\b", haystack
    )


def _resolve_model(db: Session, project: Project,
                   client: GatewayClient) -> str:
    """The analysis model (§8.2): project setting or proxy auto-resolution.

    No hardcoded names (§8.1): when the project has no ``text_model_id``
    the proxy's own list decides — first every non-text backend is skipped
    (:func:`_is_text_generation_model`), then a currently ``loaded`` text
    model is preferred (llama-swap loads on demand, but an already-loaded
    model is the one that can actually serve the batch right now). The
    choice is recorded on the project (reproducible run, §8.5).
    """
    if project.text_model_id:
        return project.text_model_id
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
    project.text_model_id = chosen["id"]
    db.add(project)
    return chosen["id"]


def _cached_runs(db: Session, project_id: str) -> dict[str, LLMRun]:
    """Completed ``llm:`` runs of the project, newest per run_type (§13.2).

    ``run_type`` is ``llm:cand:<name-digest>`` — the candidate's stable
    id — so a re-run recognises an already-classified candidate even when
    the block partition, the candidate's confidence or its entity_type
    changed in between (scope membership is intentionally stable; the
    first run's merge must not reshuffle the cache keys).
    """
    rows = (
        db.query(LLMRun)
        .filter(LLMRun.project_id == project_id,
                LLMRun.run_type.like("llm:cand:%"),
                LLMRun.status == "completed")
        .order_by(LLMRun.created_at.desc())
        .all()
    )
    cached: dict[str, LLMRun] = {}
    for row in rows:
        cached.setdefault(row.run_type, row)
    return cached


def _candidate_run_key(candidate_id: str) -> str:
    """``llm_runs`` key of one candidate's stored verdict (§13.2)."""
    return f"llm:{candidate_id}"


def run_llm_entity_classification(db: Session, job: Job) -> None:
    """Classify ambiguous/domain candidates with the analysis model (§6.2.3).

    Payload: ``project_id`` (required), ``node_id`` (optional chapter
    scope), ``model`` (optional override of the analysis model id — still
    validated against the proxy list, §8.1).
    """
    project_id = job.payload["project_id"]
    node_id = job.payload.get("node_id")
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("unknown project_id in job payload")

    # §13.1 gate: fail the job before any payload can leave the machine.
    assert_local_url(LLM_GATEWAY_BASE_URL)

    text = _scope_pages(db, project_id, node_id)
    scope = str(node_id) if node_id else "project"

    existing_rows = (
        db.query(Entity)
        .filter(Entity.project_id == project_id,
                Entity.status == "proposed")
        .all()
    )
    entities_payload = [
        {
            "canonical_source": row.canonical_source,
            "entity_type": row.entity_type,
            "confidence": (float(row.confidence)
                           if row.confidence is not None else None),
            "mention_count": (
                db.query(EntityEvidence)
                .filter(EntityEvidence.entity_id == row.id)
                .count()
            ),
        }
        for row in existing_rows
    ]

    candidates = ner_llm.select_candidates(entities_payload, text)
    if not candidates:
        job.result = {
            **(job.result or {}),
            "ner_llm": {
                "scope": scope,
                "candidates": 0,
                "blocks": 0,
                "llm_calls": 0,
                "classified": 0,
                "schema_valid_calls": 0,
                "schema_error_calls": 0,
                "cache_hits": 0,
                "schema_compliance_rate": None,
                "model": None,
                "text_sha256": sha256_hex(text),
            },
        }
        db.add(job)
        db.add(AuditLog(
            action="ner_llm_classified",
            entity="project",
            entity_id=str(project_id),
            after={"job_id": str(job.id), "scope": scope,
                   "candidates": 0, "note": "no §6.2.3 candidates"},
            created_at=datetime.utcnow(),
        ))
        return

    by_id = {c.id: c for c in candidates}
    blocks = ner_llm.chunk_blocks(
        candidates, chapter_title=job.payload.get("chapter_title"))

    client = GatewayClient()
    model = job.payload.get("model") or _resolve_model(db, project, client)

    cached_runs = _cached_runs(db, project_id)
    # §13.2 idempotency: only candidates never classified before are sent.
    # An already-classified candidate is skipped entirely — the merge below
    # is what reapplies its stored verdict, so a re-run costs zero calls
    # even when confidence/type changes reshuffled the block partition.
    fresh_ids = [cid for cid in by_id
                 if f"llm:{cid}" not in cached_runs]
    all_blocks = list(blocks)
    blocks = [b for b in blocks
              if any(cid in fresh_ids for cid in b["candidate_ids"])]
    for b in blocks:
        b["candidate_ids"] = [cid for cid in b["candidate_ids"]
                              if cid in fresh_ids]

    counters = {
        # ``blocks`` is the full block set (§13.2) — the number of blocks
        # that *would* run without idempotency. It is captured before the
        # idempotency filter so a warm run still reports the same block
        # count; ``llm_calls`` (actual calls) is 0 once everything is cached.
        "blocks": len(all_blocks),
        "llm_calls": 0,
        "schema_valid_calls": 0,
        "schema_error_calls": 0,
        # blocks dropped because every candidate is already classified
        # (§13.2): 0 on a cold run, all of ``all_blocks`` once each block
        # has been cached.
        "cache_hits": len(all_blocks) - len(blocks),
        "http_errors": 0,
        "empty_calls": 0,
    }
    classifications: list[ner_llm.Classification] = []
    merged_from_cache: list[ner_llm.Classification] = []

    for index, block in enumerate(blocks):
        counters["llm_calls"] += 1
        try:
            payload = client.chat_json(
                model=model,
                system_prompt=block["system"],
                user_prompt=block["user"],
                temperature=0.1,
                seed=12345,  # §8.1 supports_seed: riproducibilità
            )
        except GatewayInvalidJSON as exc:
            counters["schema_error_calls"] += 1
            db.add(LLMRun(
                project_id=project_id,
                run_type=f"llm:block:{block['block_hash']}",
                model_name=model,
                prompt_hash=block["prompt_sha256"],
                parameters={
                    "temperature": 0.1,
                    "block_index": index,
                    "candidate_ids": block["candidate_ids"],
                    "purpose": "ner_classification",
                },
                status="schema_error",
                error=str(exc)[:500],
            ))
            db.commit()
            continue
        except GatewayUnavailable as exc:
            counters["http_errors"] += 1
            db.add(LLMRun(
                project_id=project_id,
                run_type=f"llm:block:{block['block_hash']}",
                model_name=model,
                prompt_hash=block["prompt_sha256"],
                parameters={
                    "temperature": 0.1,
                    "block_index": index,
                    "candidate_ids": block["candidate_ids"],
                    "purpose": "ner_classification",
                },
                status="failed",
                error=str(exc)[:500],
            ))
            db.commit()
            continue

        parsed = ner_llm.parse_response(block, by_id, payload)
        # one completed run per *validated* candidate: the response is the
        # cached verdict (§8.5 stores it on the run row), and only
        # candidates that produced a valid classification are considered
        # classified — dropped entries are retried on the next run.
        output_hash = sha256_hex(json.dumps(
            payload, ensure_ascii=False, sort_keys=True))
        for classification in parsed:
            db.add(LLMRun(
                project_id=project_id,
                run_type=_candidate_run_key(classification.candidate_id),
                model_name=model,
                prompt_hash=block["prompt_sha256"],
                parameters={
                    "temperature": 0.1,
                    "max_output_tokens": LLM_GATEWAY_MAX_OUTPUT_TOKENS,
                    "block_index": index,
                    "candidate_ids": block["candidate_ids"],
                    "purpose": "ner_classification",
                    "response": payload,
                },
                output_hash=output_hash,
                status="completed",
            ))
        db.commit()

        if parsed:
            counters["schema_valid_calls"] += 1
            classifications.extend(parsed)
        else:
            counters["schema_error_calls"] += 1

    # cached verdicts re-apply through the merge (§13.2, zero calls)
    for candidate in candidates:
        cached = cached_runs.get(f"llm:{candidate.id}")
        if cached is None or not cached.parameters:
            continue
        merged_from_cache.extend(
            ner_llm.parse_response(
                {"candidate_ids": [candidate.id]}, by_id,
                cached.parameters.get("response")))
    classifications.extend(merged_from_cache)

    # ---- field-level merge + evidence with extractor='llm' (AC2) ---------
    rows_by_lower_name: dict[str, Entity] = {}
    for row in existing_rows:
        rows_by_lower_name.setdefault(
            row.canonical_source.strip().lower(), row)

    merged_count = 0
    for classification in classifications:
        row = rows_by_lower_name.get(classification.name.strip().lower())
        if row is None:
            continue
        current = {
            "entity_type": row.entity_type,
            "definition": row.definition or "",
            "confidence": (float(row.confidence)
                           if row.confidence is not None else None),
        }
        merge = ner_llm.merge_field_level(current, classification)
        if merge["patch"].get("entity_type"):
            row.entity_type = merge["patch"]["entity_type"]
        if merge["patch"].get("definition"):
            row.definition = merge["patch"]["definition"]
        row.confidence = merge["patch"]["confidence"]
        db.add(row)
        db.add(EntityEvidence(
            entity_id=row.id,
            chapter_id=node_id,
            page_number=None,
            quote_text=classification.evidence_quote,
            evidence_type=f"llm_{classification.category.lower()}",
            confidence=round(classification.confidence, 3),
            extractor=f"llm:{model}",
        ))
        merged_count += 1

    total_calls = counters["schema_valid_calls"] + counters["schema_error_calls"]
    result = {
        "scope": scope,
        "candidates": len(candidates),
        "classified": merged_count,
        "classifications": len(classifications),
        "llm_calls": counters["llm_calls"],
        "blocks": counters["blocks"],
        "schema_valid_calls": counters["schema_valid_calls"],
        "schema_error_calls": counters["schema_error_calls"],
        "cache_hits": counters["cache_hits"],
        "http_errors": counters["http_errors"],
        "schema_compliance_rate": (
            counters["schema_valid_calls"] / total_calls
            if total_calls else None
        ),
        "model": model,
        "text_sha256": sha256_hex(text),
    }
    job.result = {**(job.result or {}), "ner_llm": result}
    db.add(job)
    db.add(AuditLog(
        action="ner_llm_classified",
        entity="project",
        entity_id=str(project_id),
        after={"job_id": str(job.id), **{
            k: v for k, v in result.items() if k != "text_sha256"}},
        created_at=datetime.utcnow(),
    ))
    db.commit()
