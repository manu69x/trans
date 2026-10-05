# ADR-001 · LLM Gateway capability matrix

- **Status:** ACCEPTED
- **Date:** 2026-09-02
- **Phase:** F0 (discovery)
- **PRD refs:** §4.1, §8, §8.1, §8.3, §19.1

## 1. Context

The PRD (§8) requires the LLM Gateway to be the single source of models for
inference, with **no hardcoded model names**: the application must
interrogate the gateway at startup. §19.1 listed the concrete gateway
specification (endpoints, auth, JSON support, tokenisation, monitoring) as
an open decision. This ADR fixes those specifications based on measurements
taken against a real local gateway (a llama.cpp / llama-swap style
OpenAI-compatible proxy with on-demand model loading on consumer GPUs).

## 2. Decision

Trans targets **any OpenAI-compatible local gateway** and adapts to what it
actually exposes. The adapter assumes the lowest common denominator
verified below and degrades gracefully when optional fields are missing.

## 3. Verified behaviour

### 3.1 Endpoints, auth, health

| Field | Verified value |
|---|---|
| Base URL (OpenAI-compat) | any `http://<host>:<port>/v1` (loopback by default) |
| Auth | `Authorization: Bearer <key>`; a missing key is rejected by the gateway |
| Health | `GET /health` → `{"status":"ok", ...}`, HTTP 200 (~40 ms) |
| `/v1/models` | `{"object":"list","data":[...]}`, HTTP 200 |
| `/routes` and friends | not exposed (404) — do not rely on them |

### 3.2 Model list (`GET /v1/models`)

Model servers of the llama.cpp family expose, per model: `id`, a
human-readable `name`, a load `status` (`loaded` / `unloaded`) and provider
metadata (`owned_by`). **Important for the adapter (§8.1):** `/v1/models`
does **NOT** expose `context_length`, `max_output_tokens`,
`supports_json_schema`, etc. Therefore the system must:

- use the load status as the readiness condition (only `loaded` models serve
  batches);
- never hardcode names: interrogate `/v1/models` at startup and on explicit
  refresh (§8.1);
- derive the context window from hints in the name (e.g. "256K", "1M",
  "512K ctx") with an operator-provided override as fallback.

### 3.3 Capability matrix (PRD §8.1 format)

Values probed with `tools/gateway_probe.py`:

| Capability (§8.1) | Use | Verified value |
|---|---|---|
| `id`, `display_name`, `provider` | frontend selectors | from `/v1/models` (`id`, `name`, `owned_by`) |
| `context_window` | block budgeting | **not exposed** — derive from name hints (×1000 / ×1,000,000) or config override |
| `max_output_tokens` | batch limit | not exposed; `max_tokens` is accepted and effectively unbounded → the adapter enforces an upper bound (default 16384) |
| `supports_json_schema` | structured entity/QA output | **YES** (`response_format: {"type":"json_object"}`) — output validated against the expected schema |
| `supports_streaming` | live translation UX | **YES** (SSE, `data: [DONE]`) |
| `supports_reasoning` | must be off for translation | a `reasoning_effort` parameter is accepted, but reasoning models emit `reasoning_content` by default — it can leak into content and cost tokens → **always send `reasoning_effort: "none"` and keep schema-validated output** (§8.4/§9.5) |
| `supports_seed` | reproducibility | **YES** (`seed` accepted, HTTP 200) |
| `languages` | EN→IT filtering | not exposed — pin per project |
| `locality` | on-premise check | **on-premise** by construction: the base URL is loopback/LAN and the `local_only` policy blocks anything else (§13.1) |
| `health/latency` | readiness gating | `/health` + `loaded` status; steady-state latency of an empty completion ≈ 0.25 s; **cold start (first GPU load) ≈ 28 s** — keep timeouts generous and retry transient errors |

### 3.4 Tokenisation

Not exposed as an endpoint. For block budgeting (§4.1) the adapter uses a
conservative estimate (~4 chars/token for English + message overhead) unless
a tokenizer is available for the target model. The context window is the
hard limit; the PRD fixes **16 384 total tokens** as the working budget for
translation blocks (16k in / 16k out).

## 4. Implications

- The adapter must **interrogate `/v1/models`** and use the load status; no
  hardcoded names (PRD §8.1).
- **Context windows are derived** (name hints + config override) — never
  assumed to be exposed.
- For translation, **disable reasoning** (`reasoning_effort: "none"`) and
  **constrain output with a schema** (§8.4, §9.5): default-on reasoning is
  an unwanted cost/leak source.
- **Cold starts (~28 s) must be tolerated**: generous HTTP timeouts and
  retry on transient errors (PRD §8.4); the QE client retries too.
- `locality = on-premise`: no manuscript payload ever leaves the local
  network.

## 5. Evidence

- Tool: `tools/gateway_probe.py` (repeatable; `--help` for options).
- Run it against your own gateway to regenerate the measurements for your
  deployment.
