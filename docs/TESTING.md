# Testing

## Layout

- `backend/tests/` — the pytest suite (unit + per-phase acceptance tests).
  File names follow the PRD phases: `test_f1_*` (import/structure),
  `test_f2_*` (entities/glossary/models), `test_f3_*` (translation CAT),
  `test_f4_*` (QA/export/hardening), plus unit modules like
  `test_llm_json_parse.py`.
- `docs/benchmarks/` — parser/OCR/NER benchmark harnesses over a synthetic
  corpus (see [benchmarks/README.md](benchmarks/README.md)).
- `backend/verify_f1_e2e.py`, `backend/verify_f4_e2e.py` — end-to-end
  scripts that walk the whole pipeline (upload → parse → structure →
  translate → QA → approve → export → backup/restore) against a throwaway
  database.

## Running the suite

The tests need a local PostgreSQL reachable at `127.0.0.1:5432` with
user `trans` / password `trans` (the same credentials the dev compose
publishes). The suite:

1. creates dedicated databases (`trans_test`, `trans_hard`, ...) — it never
   touches the `trans` data volume of a running stack;
2. applies the Alembic migrations from scratch for each database;
3. fakes the LLM Gateway at the HTTP boundary (no real inference is needed
   to run the tests).

```bash
cd backend
../.venv-backend/bin/python -m pytest tests/ -q          # full suite
../.venv-backend/bin/python -m pytest tests/test_f3_translation_run.py -q
```

Or inside the backend container:

```bash
docker compose -f infra/docker-compose.yml exec backend python -m pytest tests/ -q
```

## Conventions

- **Every acceptance criterion gets a test.** New features come with tests
  named after the behaviour, not the function.
- Tests are deterministic: model calls are faked, time-dependent logic is
  injected, and each test builds its own fixture data.
- The E2E scripts print a PASS/FAIL check list and exit non-zero on the
  first failure; they are meant to be run after significant refactors.
- Frontend: TypeScript strict; `npx tsc --noEmit` (or the build) is the
  current gate — component tests are planned post-alpha.

## What is covered (highlights)

- import: text-layer extraction, OCR path, header/footer removal, page
  hashes, import reports;
- structure: proposal from bookmarks/heading patterns, editor operations,
  conservative segment regeneration after boundary edits;
- entities: preprocessing rules, BookNLP output merging, LLM NER schema
  validation, workflow (proposal → decision);
- translation: planner budgets, JSON-constrained calls, glossary/TM
  injection, anti-copy/anti-English validators, fallback model, immutable
  versions on approval;
- QA: deterministic rules, critic plumbing, QE score persistence;
- export: DOCX/EPUB/PDF/HTML/XLIFF/TMX/CSV writers, manifest + validation,
  signed URLs (valid vs tampered), watermark cases;
- hardening: log sanitizer (no manuscript text in logs), local-only URL
  policy, RBAC matrix, audit append-only trigger, backup/restore round-trip.
