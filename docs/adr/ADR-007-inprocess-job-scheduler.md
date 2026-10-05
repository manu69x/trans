# ADR-007 · Job queue — in-process scheduler is operational, Celery is not

- **Date:** 2026-09-18 · **Status:** ACCEPTED
- **Refs:** PRD §14 (async), §5.2 (parsing), ADR-002 (parser/OCR pipeline)

## Context

The original plan deferred the job-queue choice (Celery vs RQ) to this ADR.
Celery was declared as a dependency (`worker/worker/celery.py`, Redis
broker), but the path was never completed:

1. `CeleryScheduler.enqueue` imported a task module that did not exist;
   with `USE_CELERY=1` the first enqueue raised `ImportError`.
2. No Celery task covered 9 of the 12 registered `job_type`s (structure,
   chunking, NLP, LLM NER, translation, QA, TM maintenance were missing).
3. The `worker` image did not contain the `backend` package nor its
   dependencies (minio, pypdf, ...): even with routing in place, the worker
   could not execute the jobs.
4. `USE_CELERY` was not set in any environment: everything in practice ran
   on the `InProcessScheduler` (threads inside the API process).

## Decision

1. **The operational scheduler is `InProcessScheduler`** (daemon threads in
   the FastAPI process, one DB session per thread). For the PRD's
   local-only / single-host constraint this is adequate; separate queues
   (e.g. the dedicated `ocr` queue, §14) only become a real need with
   concurrent multi-process load.
2. **The Celery path fails explicitly**: `USE_CELERY=1` raises a
   `RuntimeError` with the reason, instead of an obscure `ImportError` at
   the first enqueue. No pretence of a "production" Celery setup.
3. **Boot-time recovery (§16)**: at startup, jobs left `queued`/`running`
   by a restart (dead threads without recovery) are marked `failed` with an
   explicit reason and an audit row (`job_recovered`). A job stuck in
   `running` forever is no longer a possible state.
4. The existing Celery tasks stay in the repo as the base for a future
   completion; the OCR retry bug (`raise` instead of `self.retry`) is fixed
   and OCR tasks got `soft_time_limit`/`time_limit`.

## Consequences

* Completing Celery would require: tasks for all 12 job types, a worker
  image with the `backend` package + dependencies, dispatch in
  `CeleryScheduler.enqueue`, `USE_CELERY=1` in compose, and end-to-end
  tests. As long as the load is single-host, the benefit would only be
  isolating long jobs (OCR / BookNLP up to ~30 min) from the API process.
* Long jobs block threads of the API process; the pool is unbounded (one
  thread per job). Within the local single-user usage envelope this is
  acceptable; the §13 rate limit on expensive buckets (translate / export /
  upload) mitigates abuse. A concurrency cap is a possible follow-up.
* Jobs in flight at restart are lost and reported as `failed`: the caller
  (UI) sees the state and can re-launch; `run_ref` resume (§8.4) protects
  already-completed translation blocks.
