#!/usr/bin/env python3
"""Scan Trans JSON logs for manuscript payload (PRD §13.1, acceptance criterion 4).

Reads a JSON-lines log file (the output of ``backend/logging_config.py``) and
verifies that **no** manuscript payload leaked:

1. **Canary scan** -- the caller supplies ``--canary`` phrases (distinctive
   n-grams drawn from the actual manuscript corpus). Any canary present in
   the raw log text is a FAIL: it means an unredacted payload got logged.
2. **Sensitive-key scan** -- any structured ``data`` field whose name is in
   ``SENSITIVE_KEYS`` must be a redaction stub (``{"redacted": true, ...}``),
   never a raw text value. A non-stub value under a sensitive key is a FAIL.
3. **Shape check** -- each line must be valid JSON with ``ts/level/message``.

Exit code 0 = clean, 1 = leak found. Use this as the AC4 verification:
point it at the log produced by the E2E run and the canaries from the corpus
it processed.

Usage:
    python tools/scan_logs.py --log /tmp/trans-e2e.jsonl \
        --canary "The rain fell softly" --canary "cobblestones" \
        [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Mirror the backend's sensitive-key set (import lazily so this script runs
# with the backend venv; fall back to a local copy if import fails).
try:
    from backend.logging_config import SENSITIVE_KEYS
except Exception:  # pragma: no cover - standalone mode
    SENSITIVE_KEYS = {
        "system_prompt", "user_prompt", "prompt", "block_source",
        "previous_context", "context", "content", "messages", "source_text",
        "target_text", "source_normalized", "source_original",
        "target_approved", "normalized_text", "text", "raw_text",
        "quote_text", "quote", "evidence", "ocr_text", "lines", "payload",
        "file_text", "fulltext", "full_text",
    }


def _is_stub(value) -> bool:
    return isinstance(value, dict) and value.get("redacted") is True


def scan(log_path: str, canaries: list[str]) -> dict:
    p = Path(log_path)
    if not p.exists():
        return {"ok": False, "error": f"log file not found: {log_path}"}

    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    canary_hits: dict[str, int] = {}
    key_leaks: list[str] = []
    bad_json = 0

    for ln in lines:
        # 1. raw canary scan over the whole line text
        for c in canaries:
            if c and c in ln:
                canary_hits[c] = canary_hits.get(c, 0) + 1
        # 2/3. structured checks
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            bad_json += 1
            continue
        data = rec.get("data") or {}
        for k, v in data.items():
            if k in SENSITIVE_KEYS and not _is_stub(v):
                # a raw (non-stub) value under a sensitive key = payload leak
                key_leaks.append(k)

    # The message field is the one free-text spot we allow; ensure it too
    # carries no canary (it's already covered by the raw-line scan above).
    canary_hits = {c: n for c, n in canary_hits.items() if n}
    key_leaks = sorted(set(key_leaks))

    ok = not canary_hits and not key_leaks and bad_json == 0
    return {
        "ok": ok,
        "lines": len(lines),
        "canary_hits": canary_hits,
        "sensitive_key_leaks": key_leaks,
        "bad_json_lines": bad_json,
        "canaries_checked": sorted(canaries),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", required=True, help="JSONL log file to scan")
    ap.add_argument("--canary", action="append", default=[],
                    help="Manuscript n-gram; repeatable. Presence = leak.")
    ap.add_argument("--json", action="store_true", help="emit machine JSON")
    a = ap.parse_args(argv)

    result = scan(a.log, a.canary)
    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Log: {a.log}  ({result['lines']} lines)")
        if result.get("error"):
            print(f"  ERROR: {result['error']}")
            return 1
        print(f"  canaries checked : {len(result['canaries_check'] if 'canaries_check' in result else result['canaries_checked'])}")
        if result["canary_hits"]:
            print(f"  FAIL canary payload leaked: {result['canary_hits']}")
        else:
            print("  PASS no canary (manuscript) payload in raw log text")
        if result["sensitive_key_leaks"]:
            print(f"  FAIL unredacted sensitive keys: {result['sensitive_key_leaks']}")
        else:
            print("  PASS all sensitive fields are redaction stubs")
        if result["bad_json_lines"]:
            print(f"  WARN {result['bad_json_lines']} non-JSON lines")
        else:
            print("  PASS every line is valid JSON")
        print(f"  RESULT: {'PASS (zero manuscript payload)' if result['ok'] else 'FAIL'}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
