# ADR-008 · BookNLP as a remote GPU service via the Gateway (not a local subprocess)

- **Date:** 2026-09-10 · **Status:** ACCEPTED
- **Update 2026-09-18:** the CPU fallback was **removed** — see the
  "Update" section at the end.

## Context

Entity extraction (§6.2) originally ran BookNLP as a **local CPU
subprocess** (small models). Measured limits:

- ~33 s of pipeline for a short test text; 370 s for the whole
  `test_f2_entity_extraction.py` integration test (6 tests);
- the local venv required the "small" models plus a patch for legacy
  checkpoints (`bert.embeddings.position_ids`);
- the CPU was monopolised during whole-book extraction.

Meanwhile a **"big" BookNLP service on a GPU** (Berkeley models ~1.3 GB on
CUDA, automatic model swapping with the LLMs, HTTP job-based API) can be
exposed behind the same gateway used for inference.

## Decision

1. **Default: the remote GPU service.** `nlp_runner` uses
   `backend/booknlp_service.py` (an HTTP job-based client) towards
   `LLM Gateway → /upstream/booknlp`. The gateway exposes `/upstream/*` as a
   plain forward (no wake/park/queue).
2. **Passthrough routing**: the gateway forwards `/upstream/*` to the model
   orchestrator, which routes the BookNLP upstream like any other model.
3. The client downloads the same BookNLP output files
   (`book.book/.entities/.quotes/.tokens`) into the runner's `out_dir`:
   **the downstream contract is unchanged**
   (`parse_booknlp_outputs(out_dir, file_id)` does not change).

## Consequences

- §13.1 (local-only) holds: the LLM Gateway remains the single entry point;
  no payload leaves the LAN.
- Speed: a BookNLP job takes ~9 s (including cold-loading the big models)
  vs ~33 s on CPU; the F2 integration tests drop from ~370 s to ~61 s
  (**~6×**).
- The vendored fork `vendor/booknlp-src` (with the legacy-buffer patches) is
  kept in the repo: it documents the patches needed to run BookNLP 1.0.7 on
  modern stacks and is the reference for upgrades.

## Update (2026-09-18): CPU fallback removed

Decision: **BookNLP runs only as the GPU service** (points 1 and 3 above
stand; point 2 is abrogated).

- `nlp_runner` uses only `booknlp_service`; if the service is unreachable
  the job **fails with an explicit error** (a `RuntimeError` with the
  cause) — no degradation, no alternative path. The
  `TRANS_BOOKNLP_SERVICE` kill-switch no longer exists.
- Rationale: the CPU fallback was inoperative anyway — recent transformers
  versions do not load BookNLP 1.0.7 checkpoints ("weight mismatch"),
  so the fallback only held the illusion of a plan B.
- Deploying the BookNLP service itself is out of scope of this repo: it is
  a small HTTP wrapper around the vendored BookNLP that exposes the
  job-based API the client expects.
