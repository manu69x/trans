# Architecture

How Trans works end to end: services, data model, pipelines and the module
map. The product spec behind all of this is the [PRD](PRD.md); the decision
records in [adr/](adr/README.md) explain the *why* behind the load-bearing
choices.

---

## 1. Services

| Service  | Tech                                | Role                                                            |
|----------|-------------------------------------|-----------------------------------------------------------------|
| frontend | Next.js 14, TypeScript, Tailwind    | The workbench UI (Italian) + two server-side proxies            |
| backend  | FastAPI, SQLAlchemy 2, Alembic      | The whole domain API + an in-process job scheduler (ADR-007)    |
| worker   | Celery                              | Reserved async path (not operational; ADR-007)                  |
| db       | PostgreSQL 16 + pgvector            | All relational data; embeddings for TM retrieval                |
| redis    | Redis 7                             | Broker/cache for the (dormant) Celery path                      |
| minio    | MinIO                               | S3-compatible object storage: PDFs, page rasters, export assets |

Everything is a standard public image; the backend and frontend images are
built from this repo (`backend/Dockerfile`, `frontend/Dockerfile` — see
[DEPLOYMENT.md](DEPLOYMENT.md)).

### Browser ↔ backend ↔ models

- The browser only ever talks to the Next.js origin. `/api/v1/*` is rewritten
  to the backend (`BACKEND_URL`), `/api/gateway/*` is forwarded to the LLM
  Gateway by a route handler (`LLM_GATEWAY_URL`) — both are **server-side**,
  so no gateway URL or key ever reaches the browser and there is no CORS.
- The backend is the **only** client of the LLM Gateway
  (`backend/backend/gateway_http.py`, `gateway.py`). The `local_only` policy
  (`config.assert_local_url`) runs in-process on every outbound URL.
- Long-running bulk actions bypass the Next.js proxy and hit the backend
  directly on :8000 (proxy responses truncate at ~30 s; see
  [DEPLOYMENT.md §3](DEPLOYMENT.md)).

### In-process job scheduler (ADR-007)

Long operations (import, structure detection, translation runs, entity
extraction, QA, verification, exports, backups) are **jobs**: rows in
`jobs` with a JSON payload and a JSON result, executed by an in-process
async scheduler (`scheduler.py`) with progress reporting (`progress_routes.py`
+ `translation-progress.tsx`). Celery/Redis remain wired but are not the
operational path — one less moving part for a single-machine deployment.

---

## 2. Data model

Schema truth lives in `backend/migrations/versions/` (Alembic; 18 revisions).
Main aggregates (models in `backend/backend/models/`):

| Aggregate                | Tables / fields (essentials)                                                        |
|--------------------------|--------------------------------------------------------------------------------------|
| **Project**              | `projects` — title, language pair (EN→IT), model settings (per-project default model, temperature, top_p/k, max tokens), style/profile choices |
| **Document**             | `documents` — uploaded PDF (object in MinIO), hash; `document_pages` — per-page text + coordinates + OCR confidence + source flags; `import_reports` |
| **Structure**            | `structure_nodes` — tree of parts/chapters/scenes with `kind`, `normalized_title`, `ordinal`, `status` (proposed→confirmed), edited via the structure editor surface |
| **Segments**             | `translation_units` — one row per segment: `source_text` (+`source_hash`, `source_flags`), `target_text`, `status` (`machine_draft` → `approved`/`rejected`), stable per-project `numero`, QE scores (`is_italian`, `is_translated`, `is_english`), `quality_score`, snapshot ids |
| **Versions**             | `translation_unit_versions` — immutable version chain; approved text is never overwritten, every edit appends a version |
| **Entities**             | `entities` + workflow fields — name, aliases, type (person/place/artifact/species/organization/...), gender, number, per-target-language translation, decision status, evidence spans |
| **Glossary / TM**        | `glossary` entries (term → approved translation, notes, status) and TM links (`tm_segment_link`) to approved pairs; pgvector embeddings for retrieval |
| **LLM runs**             | `llm_runs` — every model call recorded with input/output hash and full parameters (replayable, §8.4/§16) |
| **QA**                   | `qa_issues` — deterministic + LLM-critic findings with span/comment fields          |
| **Jobs**                 | `jobs` — type, payload, status machine (queued/running/completed/failed/cancelled), progress + result JSON |
| **Audit**                | append-only audit log (trigger-enforced): auth events, approvals, exports, admin actions |

**Immutability rule** (PRD §5.8): LLM output enters as `machine_draft`;
only a human approval moves it forward; approved text is immutable — edits
create a new version. The append-only audit trail records who/what/when.

---

## 3. Pipelines

### 3.1 Import (PDF → pages) — `l1_runner.py`, `parsing/l1.py`, `parsing/ocr.py`

1. Upload → MinIO; `documents` row with the file hash.
2. **L1 layout-aware extraction** (PyMuPDF): per-page text blocks with
   coordinates, reading order, page hash; digital PDFs end here.
3. **Scans** (no text layer) → rasterisation + local OCR with per-page
   confidence; results stored with `source_flags` so downstream stages know
   the provenance.
4. `import_reports` summarises what was extracted/OCR-ed.

### 3.2 Structure detection (pages → chapters) — `structure_runner.py`, `parsing/structure.py`, `structure_editor.py`

- Bookmarks/TOC when present, heading-pattern heuristics otherwise →
  proposed `structure_nodes` tree (parts/chapters/scenes).
- The user confirms/corrects in the UI (merge/split/rename/reorder, fix
  boundaries). Re-generating segments after a boundary fix only touches the
  affected range, preserving approved work.
- Repeated running headers/footers are detected and excluded from the body.

### 3.3 Segmentation (chapters → translation units) — `parsing/chunking.py`, `chunking_runner.py`, `segment_numbering.py`

- Chapters are split into translation units within a target block budget
  (default 16k tokens) with sentence-boundary chunking; every segment gets a
  stable per-project `numero` used by filters, ordering and QA views.

### 3.4 Entities (literary knowledge base) — `nlp_runner.py`, `booknlp_service.py`, `llm_ner_runner.py`, `parsing/ner_llm.py`, `parsing/entities.py`

Two complementary extractors, both local:

- **BookNLP** (vendored in `vendor/booknlp-src/`, served as a local GPU
  service, ADR-008): characters, quotations, coreference, supersense — the
  literary backbone.
- **LLM NER pass** (structured JSON output through the gateway): domain
  categories from PRD §6.2.3 (invented artifacts, species, organizations...)
  with schema validation and retry; candidate merging with the BookNLP
  output (`parsing/entities.py`).

The editor (`entita` page) then curates aliases, gender, number and the
approved translation per entity — feeding glossary constraints at
translation time.

### 3.5 Glossary, TM and retrieval — `glossary.py`, `translation/embedding.py`, `translation/tm_retrieval.py`, `tm_maintenance.py`

- Controlled glossary per project (term, approved translation, status,
  notes); entity translations can be promoted into it.
- Translation memory of approved pairs, embedded with a local model into
  pgvector; retrieval injects the k nearest approved pairs into the
  translation prompt (terminology-anchored translation, PRD §7).

### 3.6 Translation — `translation/planner.py`, `translation/runner.py`, `translation/validators.py`, `gateway_http.py`

1. **Planning**: group segments into blocks within the token budget, attach
   local context (previous segment tail, entity/glossary constraints, TM
   neighbours, style profile prompt for the genre, PRD §9).
2. **Execution**: JSON-constrained chat completions through the gateway
   (`chat_json`); every call recorded in `llm_runs`; RPS rate limiting and
   retry with backoff on 429/5xx; model unavailable → the batch is
   **suspended** and the user is asked, never silently re-routed (§8.4).
3. **Validation**: per-block validators flag drafts that copy the source
   (`is_english`-style anti-copy), stay English, or break markup; a
   **fallback model** (`TRANS_FALLBACK_TRANSLATION_MODEL`, optional) is
   retried automatically for untranslated drafts.
4. Output lands as `machine_draft` on `translation_units`. Bulk translation
   keys ("translate selection", single-segment re-run) live in
   `translation_routes.py`.

### 3.7 QA — `qa/runner.py`, `qa/deterministic.py`, `qa/critic.py`, `qa/quality_estimation.py`, `verify_handler.py`, `segment_verify.py`

Three layers over drafts:

- **Deterministic checks** (numbers, quotes balance, markup integrity,
  glossary violations, unedited source).
- **LLM critic** — a second model pass per segment with a rubric; produces
  `qa_issues` with spans/comments.
- **QE scoring** — the small quality-estimation endpoint
  (`qe_client.py`, optional, see `deploy/qe-service/`) computes
  `is_italian` / `is_translated` / `is_english` per segment on a
  head+tail excerpt. The review UI (`traduzione` page) surfaces the scores
  as IT / TR / DIFF columns with filters (`lib/qe.ts`,
  `components/qe-highlight.tsx`).

Nothing is auto-applied: findings are advisory until a human approves.

### 3.8 Exports — `export_routes.py`, `export/*`

- **Editorial**: DOCX, EPUB (standard + "clone" variants that mimic the
  source typography with bundled DejaVu fonts), PDF, styled HTML, and a live
  preview page (`preview.py` + `anteprima`).
- **CAT round-trip**: XLIFF, TMX, CSV (bilingual), with `reimport.py` to
  take external edits back as new versions.
- Exports only include **approved** content by default, record a manifest
  (`manifest.py`), are integrity-checked (`validate.py`), and asset URLs are
  signed (`signed_urls.py`) with retention handling (`retention.py`,
  `repair_copies.py`).

### 3.9 Backup / DR — `backup.py`

Versioned backup of DB + MinIO assets with a manifest and hash verification;
restore into a clean DB is exercised by the E2E test.

---

## 4. Cross-cutting

| Concern           | Where                                   | Notes                                                                       |
|-------------------|-----------------------------------------|-----------------------------------------------------------------------------|
| Auth & RBAC       | `security.py`, `auth_routes.py`, `rbac.py`, `seed.py` | JWT access+refresh tokens; 5 roles (admin, project_manager, translator, revisor, qa_reader); per-route permission matrix; idempotent admin seed |
| Local-only policy | `config.py` (`assert_local_url`)        | Outbound URLs must be local or the call is refused (§13.1)                   |
| Log sanitization  | `logging_config.py`                     | Structured JSON logs; manuscript text scrubbed (§13.2)                       |
| Crypto at rest    | `crypto_at_rest.py`                     | Storage encryption keyed from a file outside the repo (prod)                 |
| Audit             | `audit.py` + append-only trigger        | Every sensitive action recorded, tamper-evident                              |
| Rate limiting     | `rate_limit.py`                         | Per-client buckets (uses real client IP behind the TLS proxy)                |

---

## 5. Backend module map

```
backend/backend/
├── main.py               app factory: routers, CORS, boot-time gateway sync
├── config.py             env-driven settings + local_only policy
├── db.py, seed.py        engine/session; idempotent admin + RBAC seed
├── security.py, rbac.py  JWT issue/verify; role→permission matrix
├── gateway.py            gateway client+adapter (capability matrix, llm_runs,
│                         suspend/retry semantics)  [tests fake the client]
├── gateway_http.py       the only HTTP path to models: list_models, chat_json
├── gateway_routes.py     /api/v1/gateway/* (models, health, test-run, refresh)
├── qe_client.py          QE endpoint client (3 probabilities, JSON-constrained)
├── scheduler.py          in-process job scheduler (ADR-007)
├── l1_runner.py          import pipeline (L1 extraction + OCR + reports)
├── structure_runner.py, structure_editor.py   chapters proposal/editing
├── chunking_runner.py, segment_numbering.py   segmentation + stable numbering
├── nlp_runner.py, booknlp_service.py, llm_ner_runner.py   entity extraction
├── glossary.py, entity_io.py                  glossary + entity import/export
├── translation/{planner,runner,validators,embedding,tm_retrieval}.py
├── qa/{deterministic,critic,quality_estimation,runner,categories}.py
├── verify_handler.py, segment_verify.py       bulk QE verification jobs
├── export/*              docx/epub/pdf/html/xliff/tmx/csv + manifest/validate
├── preview.py, typography.py, repair_copies.py, retention.py
├── backup.py, crypto_at_rest.py, signed_urls.py
└── audit.py, logging_config.py, rate_limit.py, tokens.py
```

## 6. Frontend module map

```
frontend/src/
├── app/
│   ├── page.tsx                 dashboard (projects overview)
│   ├── progetti/                project list + create
│   ├── import/                  upload + import report
│   ├── segmenti/                structure & segments review
│   ├── entita/                  entity KB editor (aliases, gender, decisions)
│   ├── traduzione/              the CAT workbench (drafts, filters, QA panel,
│   │                            bulk translate/verify/approve, QE columns)
│   ├── qa/                      QA issues triage
│   ├── export/                  export matrix + previews
│   ├── anteprima/               HTML preview of approved content
│   ├── prompt-modelli/          per-project model & prompt settings
│   ├── audit/                   audit trail viewer
│   ├── health/ info/ login/     ops pages
│   └── api/{v1,gateway}/...     server-side proxies (no CORS, secrets server-side)
├── components/                  model-selector, qe-highlight, pdf-viewer,
│                                translation-progress, auth-guard, ...
└── lib/                         api.ts (typed client + longApi bypass),
                                 api-types.ts, qe.ts (QE head/tail + highlighting),
                                 model-budget.ts, auth.ts
```

> The UI language is Italian by design (the primary users are Italian
> literary translators); pages map 1:1 to the workflow steps above.

## 7. Extension points

- **Models**: anything OpenAI-compatible behind the gateway; the capability
  matrix is discovered, never configured.
- **QE**: any endpoint answering `/health` + `/v1/chat/completions`
  (`QE_BASE_URL` / `QE_MODEL`), or the reference bridge in
  `deploy/qe-service/`.
- **Export formats**: add a writer under `backend/backend/export/` and
  register it in `export_routes.py`.
- **Entity categories**: extend the LLM NER schema (`parsing/ner_llm.py`)
  and the UI type map.
