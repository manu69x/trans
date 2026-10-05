# Architecture Decision Records

Decision records for the load-bearing technical choices. Numbering follows
the PRD phases; gaps in the numbering are records that were folded into
other documents.

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](ADR-001-gateway-capability-matrix.md) | LLM Gateway capability matrix — what an OpenAI-compatible local gateway really exposes, and how the adapter copes | Accepted |
| [ADR-002](ADR-002-parser-pipeline.md) | Four-level parsing / OCR pipeline with conditional escalation (L1 PyMuPDF → L2 Docling → L3 OCR → L4 layout detection) | Accepted |
| [ADR-003](ADR-003-db-schema.md) | Final DB schema: Alembic-only, PostgreSQL 16 + pgvector, ER diagram and delta vs the PRD | Accepted |
| [ADR-007](ADR-007-inprocess-job-scheduler.md) | Job queue: in-process scheduler is operational, Celery stays dormant | Accepted |
| [ADR-008](ADR-008-booknlp-gpu-service.md) | BookNLP as a remote GPU service via the Gateway; CPU fallback removed | Accepted |
