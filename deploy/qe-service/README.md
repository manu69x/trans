# QE service — translation quality estimation

Segment-level quality estimation (QE) is the feature behind the
"Verify" bulk actions on the Translation page (`POST /projects/{id}/verify-translations`).
For every translated segment the platform asks a small LLM endpoint three
closed questions and stores the resulting probabilities on the segment:

* `is_italian` — is the target written in Italian?
* `is_translated` — is the target a complete, faithful rendering of the source?
* `is_english` — is the target actually still English (untranslated draft)?

Scores land in `translation_units.is_italian / is_translated / is_english`
and drive the IT / TR / DIFF filters and highlighting in the UI.

## Wiring it up

The client lives in `backend/backend/qe_client.py`. Two settings control it:

| Env var        | Meaning                                                        | Default (derived)                     |
|----------------|----------------------------------------------------------------|---------------------------------------|
| `QE_BASE_URL`  | OpenAI-compatible base URL of the QE endpoint                  | `{LLM_GATEWAY_BASE_URL minus /v1}/upstream/qe` |
| `QE_MODEL`     | model id sent in the completion payload                        | `qe-verify`                            |

The default assumes your LLM gateway exposes the QE model as an upstream
route named `qe` (e.g. llama-swap `upstreams` entry), so one gateway serves
both the translation models and the QE model, and **no extra service is
needed**.

If instead the QE model runs on its own plain OpenAI-compatible server,
point `QE_BASE_URL` directly at it:

```bash
QE_BASE_URL=http://127.0.0.1:8081
QE_MODEL=qe-verify
```

Any server answering `GET /health` and `POST /v1/chat/completions` works.

## Standalone bridge (this folder)

`app.py` is a tiny FastAPI *bridge* for the second setup: it exposes the
`/health` + `/v1/chat/completions` surface and forwards the QE prompt to a
configurable upstream (llama.cpp `llama-server`, vLLM, LM Studio, ...).

```bash
pip install fastapi uvicorn httpx pydantic
UPSTREAM_BASE_URL=http://127.0.0.1:8080/v1 \
UPSTREAM_MODEL=qe-verify \
uvicorn app:app --host 127.0.0.1 --port 8081
```

| Env var              | Meaning                                   | Default                     |
|----------------------|-------------------------------------------|-----------------------------|
| `UPSTREAM_BASE_URL`  | OpenAI-compatible base URL of the backing server | `http://127.0.0.1:8080/v1` |
| `UPSTREAM_API_KEY`   | bearer token sent upstream                | `sk-local-dev-change-me`    |
| `UPSTREAM_MODEL`     | model id used for completions             | `qe-verify`                 |

## The prompt

One chat completion with `response_format: json_object` (llama.cpp servers
enforce it as a GBNF grammar, so the JSON is guaranteed) and, when
supported, `enable_thinking: false`. The model must answer exactly:

```json
{"is_italian": 0.99, "is_translated": 0.98, "is_english": 0.0}
```

The backend clamps the values to [0, 1] and rounds to 4 decimals, so any
reasonably instructable small model (1–8 B) works. To keep prompts short
the platform only ever sends the **first and last sentence** of source and
target (see `backend/backend/verify_handler.py`).
