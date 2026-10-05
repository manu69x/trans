"""BookNLP service client (GPU via LLM Gateway).

PRD §13.1 (local-only) is preserved: the service runs on a machine inside
the LAN and is reached ONLY through the local LLM Gateway (the sole allowed
LLM/NLP path, ADR-001).

Contract: the gateway exposes the BookNLP service under ``/upstream/booknlp``
(a small FastAPI wrapper around BookNLP running on a GPU box; deploying the
wrapper is out of scope of this repo, see ADR-008).

API:
* POST /process  {"text": "...", "book_id": "x"} → {"job_id", "status"}
* GET  /jobs/{id} → {"status": queued|running|done|error, ...}
* GET  /jobs/{id}/files/{name} → raw file content
"""
from __future__ import annotations

import os
import time

import httpx

from .config import LLM_GATEWAY_BASE_URL, LLM_GATEWAY_API_KEY

BOOKNLP_SERVICE_TIMEOUT = int(os.getenv("BOOKNLP_SERVICE_TIMEOUT", "1800"))
BOOKNLP_POLL_INTERVAL = float(os.getenv("BOOKNLP_POLL_INTERVAL", "2.0"))


def _base_url() -> str:
    base = LLM_GATEWAY_BASE_URL.rstrip("/")
    # the proxy mounts llama-swap under /v1; the service routes live on the
    # bare /upstream path of the same host.
    return base.rsplit("/v1", 1)[0] + "/upstream/booknlp"


def _headers() -> dict:
    return {"Authorization": f"Bearer {LLM_GATEWAY_API_KEY}"}


def check_health(timeout: float = 30.0) -> dict:
    """Service health (this also triggers the on-demand llama-swap load)."""
    resp = httpx.get(f"{_base_url()}/health", headers=_headers(),
                     timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def run_booknlp(text: str, out_dir: str, book_id: str = "book") -> str:
    """Run BookNLP on the GPU service; write its outputs into *out_dir*.

    Returns the BookNLP file id (``book_id``), i.e. the same contract the
    local subprocess path had (``parse_booknlp_outputs(out_dir, file_id)``
    consumes the files afterwards). Raises on service error or timeout.
    """
    base = _base_url()
    headers = _headers()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=30.0)) as client:
        # 0. health (loads the model on demand when swapped out)
        resp = client.get(f"{base}/health", headers=headers)
        resp.raise_for_status()

        # 1. submit
        resp = client.post(f"{base}/process", headers=headers, json={
            "text": text, "book_id": book_id})
        resp.raise_for_status()
        job_id = resp.json()["job_id"]

        # 2. poll until done/error (the big-model GPU load can take ~10s;
        #    long books take minutes — the caller sets the overall timeout)
        deadline = time.monotonic() + float(
            os.getenv("BOOKNLP_SERVICE_TIMEOUT", "1800"))
        while True:
            status = client.get(f"{base}/jobs/{job_id}", headers=headers).json()
            st = status.get("status")
            if st == "done":
                break
            if st == "error":
                raise RuntimeError(
                    "BookNLP service job failed: "
                    + str(status.get("error") or status)[:300])
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"BookNLP service job not done in time: {job_id}")
            time.sleep(BOOKNLP_POLL_INTERVAL)

        # 3. fetch every produced file into out_dir
        produced = [f"{book_id}.book", f"{book_id}.entities",
                    f"{book_id}.quotes", f"{book_id}.tokens"]
        for name in produced:
            resp = client.get(f"{base}/jobs/{job_id}/files/{name}",
                              headers=headers)
            if resp.status_code == 200:
                with open(os.path.join(out_dir, name), "wb") as fh:
                    fh.write(resp.content)
            # a missing optional file (e.g. no quotes) is fine
    return book_id
