# Contributing

Thanks for your interest in Trans! The project is in **alpha**: the fastest
way to help right now is to use it, report issues and share benchmark
results. Code contributions are welcome — these are the ground rules.

## Ground rules (non-negotiable)

1. **Local-only.** No feature may send manuscript text, glossaries,
   embeddings or telemetry to a cloud endpoint. Inference goes through the
   configured LLM Gateway only; the `local_only` policy (`backend/backend/config.py`)
   must keep rejecting anything else.
2. **Human approval.** LLM output is always `machine_draft`. Never add a
   path where machine output becomes "approved" without an explicit user
   action, and never mutate an approved version — every edit creates a new
   version.
3. **No manuscript text in logs.** Keep the JSON log sanitizer
   (`logging_config.py`) intact; add tests if you touch logging.
4. **Model-agnostic.** Do not hardcode model names; everything model-related
   is discovered from the gateway or configured via env/settings.

## Development conventions

- **Stack**: FastAPI (Python 3.12+), PostgreSQL 16 + pgvector, Redis, MinIO,
  React + TypeScript + Next.js + Tailwind, Docker Compose. The schema is
  Alembic-only: no raw SQL migrations, no edits to applied revisions — add a
  new revision instead.
- **Style**: Python formatted with black + linted with ruff, type hints
  everywhere; TypeScript via eslint + prettier.
- **Tests**: every acceptance criterion gets a test (see
  [docs/TESTING.md](docs/TESTING.md)). Run the backend suite locally before
  opening a PR.
- **Commits**: conventional commits (`feat:`, `fix:`, `docs:`, `chore:`,
  `refactor:`), one logical change per commit.
- **Docs**: user-facing behaviour changes update the relevant docs
  (README / ARCHITECTURE / DEPLOYMENT) in the same PR. Significant
  architectural choices get an ADR in `docs/adr/` (see the existing ones for
  the format).
- **DB**: the backend runs `alembic upgrade head` at boot; test suites
  create their own throwaway databases and never touch your data volumes.

## Workflow

1. Fork / branch from `main`.
2. Make the change + tests + docs.
3. `cd backend && ../.venv-backend/bin/python -m pytest tests/ -q`
4. Frontend changes: `npm run build` (or `npx tsc --noEmit`) must pass.
5. Open a PR describing the behaviour change and how to test it.

## Reporting issues

Include: what you did, what you expected, what happened, the backend log
segment (it is sanitized — safe to share), your OS/Docker versions and — for
model issues — the gateway response of `GET /v1/models` (model ids are not
sensitive).

## A note on the UI language

The web UI is in Italian and code comments are mixed Italian/English — a
legacy of the project's origin. Issues and PRs are in English. Translating
the UI is tracked as a post-alpha goal; contributions there are very
welcome.
