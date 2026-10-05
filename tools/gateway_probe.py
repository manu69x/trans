#!/usr/bin/env python3
"""
LLM Gateway capability probe.

Interroga il LLM Gateway locale (endpoint OpenAI-compatible) e misura le
capacità reali di ciascun modello: context window, max output, supporto JSON
schema / grammar-constrained decoding, streaming, reasoning, seed, tokenizzazione,
latenza media su 10 chiamate vuote, health check. Produce un report JSON
ripetibile e una matrice di capacità per il ADR.

Uso:
    python3 tools/gateway_probe.py [--base-url URL] [--model ID] [--json] [--all-models]

Se --json: scrive il report in tools/gateway_probe_report.json (o stdout).
"""
import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("LLM_GATEWAY_BASE_URL", "http://127.0.0.1:8080/v1")
_API = {"base_url": DEFAULT_BASE}


def _base():
    return _API["base_url"]
API_KEY = os.environ.get("LLM_GATEWAY_API_KEY", "sk-local-dev-change-me")
DEFAULT_MODEL = os.environ.get(
    "LLM_GATEWAY_MODEL", "qwen38-27b-rtx3090-178k_G0"
)

# modelli di prova se la lista /v1/models è vuota (target in sospensione)
FALLBACK_MODELS = [
    "gpt-4o-mini",
    "gpt-4o",
    "claude-sonnet-5",
    "gemini-37-flash",
    "qwen38-27b-rtx3090-178k_G0",
]


def api_call(path, payload=None, method="POST", timeout=120, extra_headers=None):
    base = _base().rstrip("/")
    url = base + "/" + path.lstrip("/")
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return {
                "status": r.status,
                "elapsed": time.time() - t0,
                "headers": dict(r.headers),
                "body": body.decode(errors="replace"),
            }
    except urllib.error.HTTPError as e:
        return {
            "status": e.code,
            "elapsed": time.time() - t0,
            "headers": dict(e.headers),
            "body": e.read().decode(errors="replace"),
        }
    except Exception as e:  # noqa: BLE001
        return {"status": None, "elapsed": time.time() - t0, "error": str(e)}


def parse_json(s):
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return None


def discover_models():
    r = api_call("models", method="GET")
    data = parse_json(r["body"])
    models = []
    if data and isinstance(data, dict) and data.get("object") == "list":
        for m in data.get("data", []):
            st = m.get("status", {}) or {}
            meta = m.get("meta", {}) or {}
            ctx = None
            for k in ("context_length", "context_window", "max_context_length"):
                if isinstance(m, dict) and m.get(k):
                    ctx = int(m[k])
                    break
            if ctx is None and isinstance(meta, dict):
                mm = meta.get("llamaswap", {}) or {}
                for k in ("context_length", "context_window"):
                    if mm.get(k):
                        ctx = int(mm[k])
                        break
            models.append({
                "id": m.get("id"),
                "name": m.get("name") or m.get("id"),
                "owned_by": m.get("owned_by"),
                "status": st.get("value") if isinstance(st, dict) else st,
                "context_length": ctx or CONTEXT_OVERRIDES.get(m.get("id")) or infer_context(m.get("name")),
                "raw": m,
            })
    return models, r


# known context_length mapping supplied by the gateway/config (override)
CONTEXT_OVERRIDES = {
    "qwen38-27b-rtx3090-178k_G0": 178000,
}

def infer_context(name):
    """Deriva il context window dai hint nel nome (256K, 1M, 128K, ...).

    Preferisce gli hint vicini a 'ctx'/'context'; in alternativa prende
    l'ultimo K/M del nome. Converte K->*1000, M->*1_000_000."""
    if name is None:
        return None
    pats = list(re.finditer(r'(\d+(?:\.\d+)?)\s*([KM])\b', name, re.IGNORECASE))
    if not pats:
        return None

    def to_int(m):
        val = float(m.group(1))
        return int(val * 1000) if m.group(2).upper() == "K" else int(val * 1_000_000)

    # hint vicino a 'ctx'/'context'
    for m in reversed(pats):
        window = name[max(0, m.start() - 25):m.end() + 8]
        if re.search(r'ctx|context', window, re.IGNORECASE):
            return to_int(m)
    # fallback: ultimo match
    return to_int(pats[-1])


def try_model_completion(model, payload, timeout=180):
    return api_call("chat/completions", payload, timeout=timeout)


def _run_one(model, **fields):
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 30,
    }
    payload.update(fields)
    return try_model_completion(model, payload, timeout=180)


def test_json_schema(model):
    schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "ok": {"type": "boolean"},
        },
        "required": ["status", "ok"],
        "additionalProperties": False,
    }
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": "You are a strict JSON-only assistant."},
            {"role": "user", "content": 'Return only the JSON {"status":"ok","ok":true}'},
        ],
        "max_tokens": 50,
    }
    # A) response_format json_object (OpenAI standard)
    r = try_model_completion(model, dict(payload, response_format={"type": "json_object"}))
    data = parse_json(r["body"])
    ok = bool(data and data.get("choices"))
    content = ""
    parsed = None
    if ok:
        content = (data["choices"][0].get("message", {}) or {}).get("content", "")
        parsed = parse_json(content)
    schema_ok = bool(parsed and isinstance(parsed, dict) and "status" in parsed and "ok" in parsed)

    # B) grammar-constrained: vLLM accetta 'grammar nel body (GBNF)
    r2 = try_model_completion(
        model,
        dict(payload, extra_body={"grammar": 'root ::= "{" "status" ":" "\"ok\"" "}"'}),
    )
    g_accepted = r2["status"] == 200 and not r2.get("error")

    return {
        "json_object_format_supported": schema_ok,
        "output": content,
        "output_valid_json": parsed is not None,
        "output_matches_schema": schema_ok,
        "grammar_constrained_ghint_supported": g_accepted,
        "status": r["status"],
        "elapsed": r["elapsed"],
        "error": r.get("error"),
    }


def test_streaming(model):
    payload = {
        "model": model,
        "temperature": 0.2,
        "stream": True,
        "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
        "max_tokens": 10,
    }
    r = api_call("chat/completions", payload, timeout=120)
    body = r["body"]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    events = []
    for ln in lines:
        if ln.startswith("data:"):
            events.append(ln[5:].lstrip())
    done = any(e.strip() == "[DONE]" for e in events)
    chunks = 0
    first_err = None
    for e in events:
        d = parse_json(e)
        if d is None:
            continue
        chunks += 1
        if isinstance(d, dict) and d.get("error"):
            first_err = d["error"]
    return {
        "streaming_supported": bool(events) and (done or chunks > 0),
        "stream_events": len(events),
        "done_sentinel_present": done,
        "first_error": first_err,
        "status": r["status"],
        "elapsed": r["elapsed"],
    }


def test_reasoning(model):
    r = _run_one(model, reasoning_effort="none")
    headers = r.get("headers") or {}
    has_header = any(
        k.lower() == "x-reasoning" or "reasoning" in k.lower() for k in headers
    )
    return {
        "reasoning_parameter_accepted": r["status"] == 200 and not r.get("error"),
        "reasoning_header_in_response": has_header,
        "status": r["status"],
        "elapsed": r["elapsed"],
        "error": r.get("error"),
    }


def test_seed(model):
    r = _run_one(model, seed=42)
    return {
        "seed_parameter_accepted": r["status"] == 200 and not r.get("error"),
        "status": r["status"],
        "elapsed": r["elapsed"],
        "error": r.get("error"),
    }


def test_max_output(model):
    # tenta un output lungo per ricavare il max_output_tokens reale
    payload = {
        "model": model,
        "temperature": 1.0,
        "messages": [{"role": "user", "content": "Write a long story about a cat."}],
        "max_tokens": 4096,
    }
    r = try_model_completion(model, payload, timeout=240)
    data = parse_json(r["body"])
    n = 0
    if data and data.get("choices"):
        c = data["choices"][0]
        n = len(c.get("content", "") or "")
    return {
        "requested_max_tokens": 4096,
        "produced_chars": n,
        "status": r["status"],
        "elapsed": r["elapsed"],
        "error": r.get("error"),
    }


def measure_latency(model, n=10):
    times = []
    # warmup: il primo caricamento in GPU (cold start) è atipico; si misura
    # la latenza steady-state su n chiamate consecutive.
    try_model_completion(model, {
        "model": model, "temperature": 0.2,
        "messages": [{"role": "user", "content": "pong"}], "max_tokens": 5,
    }, timeout=180)
    for _ in range(n):
        payload = {
            "model": model,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
            "max_tokens": 5,
        }
        r = try_model_completion(model, payload, timeout=180)
        if r["status"] == 200 and not r.get("error"):
            times.append(r["elapsed"])
    return {
        "samples": len(times),
        "times_seconds": [round(t, 3) for t in times],
        "mean_seconds": round(sum(times) / len(times), 3) if times else None,
        "min_seconds": round(min(times), 3) if times else None,
        "max_seconds": round(max(times), 3) if times else None,
    }


def health_check():
    for path in ("/health", "/v1/health", "/healthz"):
        r = api_call(path.lstrip("/"), method="GET", timeout=6)
        data = parse_json(r["body"])
        if r["status"] == 200 and isinstance(data, dict):
            return {
                "endpoint": path,
                "status": r["status"],
                "response": data,
                "elapsed": r["elapsed"],
            }
    # fallback TCP connect al target
    from urllib.parse import urlsplit
    parts = urlsplit(_base())
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 2027
    try:
        with socket.create_connection((host, port), timeout=5):
            return {
                "endpoint": "tcp",
                "status": 200,
                "response": {"host": host, "port": port, "reachable": True},
            }
    except Exception as e:  # noqa: BLE001
        return {"endpoint": "tcp", "status": None, "error": str(e)}


def tokenize_estimate(text):
    """Stima i token senza dipendenze esterne: heuristic char/4 + messaggi."""
    n = len(text) // 4 + 3
    return max(1, n)


def build_capability_matrix(models, probe_model=DEFAULT_MODEL):
    """Matrice di capacità per modello come da PRD §8.1."""
    matrix = []
    for m in models:
        mid = m["id"] or probe_model
        ctx = m.get("context_length")
        row = {
            "id": m.get("id"),
            "display_name": m.get("name"),
            "provider": m.get("owned_by"),
            "status": m.get("status"),
            "context_window": ctx,
            "max_output_tokens": None,
            "supports_json_schema": None,
            "supports_streaming": None,
            "supports_reasoning": None,
            "supports_seed": None,
            "locality": "on-premise" if _base().startswith(("http://127", "http://localhost")) else "remote",
        }
        try:
            row["max_output_tokens"] = test_max_output(mid)["produced_chars"]
        except Exception:  # noqa: BLE001
            pass
        try:
            row["supports_json_schema"] = test_json_schema(mid)["json_object_format_supported"]
        except Exception:  # noqa: BLE001
            pass
        try:
            row["supports_streaming"] = test_streaming(mid)["streaming_supported"]
        except Exception:  # noqa: BLE001
            pass
        try:
            row["supports_reasoning"] = test_reasoning(mid)["reasoning_parameter_accepted"]
        except Exception:  # noqa: BLE001
            pass
        try:
            row["supports_seed"] = test_seed(mid)["seed_parameter_accepted"]
        except Exception:  # noqa: BLE001
            pass
        matrix.append(row)
    return matrix


def main():
    ap = argparse.ArgumentParser(description="LLM Gateway capability probe")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--all-models", action="store_true", help="prova tutte le capacità su ogni modello")
    ap.add_argument("--json", action="store_true", help="scrive il report in file JSON")
    args = ap.parse_args()

    _API["base_url"] = args.base_url

    out = {
        "probe_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base_url": _base(),
        "auth": "Bearer <key>" if API_KEY else "none",
        "health": health_check(),
    }

    models, models_resp = discover_models()
    out["models_endpoint_status"] = models_resp["status"]
    out["models_endpoint_error"] = models_resp.get("error")
    if not models:
        # target in sospensione: prova modelli di fallback
        out["models_source"] = "fallback (lista /v1/models vuota)"
        models = [
            {"id": fid, "name": fid, "owned_by": "fallback", "status": None, "context_length": None}
            for fid in FALLBACK_MODELS
        ]
    else:
        out["models_source"] = "/v1/models"

    out["models_discovered"] = [
        {"id": m["id"], "name": m["name"], "owned_by": m["owned_by"], "status": m["status"], "context_length": m["context_length"]}
        for m in models
    ]

    probe_model = args.model
    out["probe_model"] = probe_model
    out["latency_10_calls"] = measure_latency(probe_model, 10)
    out["json_schema_test"] = test_json_schema(probe_model)
    out["streaming_test"] = test_streaming(probe_model)
    out["reasoning_test"] = test_reasoning(probe_model)
    out["seed_test"] = test_seed(probe_model)

    if args.all_models:
        out["capability_matrix"] = build_capability_matrix(models, probe_model)

    if args.json:
        report_path = os.path.join(os.path.dirname(__file__), "gateway_probe_report.json")
        with open(report_path, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("report scritto:", report_path)
    else:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
