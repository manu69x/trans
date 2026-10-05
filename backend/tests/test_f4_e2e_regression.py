"""Regression tests for the F4 hardening unblocking fixes (2026-09-09).

Covers the three root causes found while driving verify_f4_e2e.py to 24/24:

1. the §9.4 prompt rendered the literal ``{{segment_batch_with_ids}}``
   placeholder instead of the block's segments (planner);
2. small local models deviate from the §9.5 wire schema (bare list /
   ``translation`` instead of ``target_text``) -- now normalised in
   ``runner._normalize_model_response``;
3. ``_save_segments`` mixed UUID/str dict keys and tried to INSERT a row that
   segmentation had already created (translation_units_pkey violation).
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f4reg")
os.environ.setdefault("LOCAL_ONLY", "1")

from backend.translation.planner import (  # noqa: E402
    build_block_plan,
    render_segment_batch,
)
from backend.translation.runner import _normalize_model_response  # noqa: E402


# --- 1. the prompt carries the real segments, never the literal placeholder -
def test_prompt_contains_rendered_segment_batch():
    segs = [
        {"segment_id": "s1", "source_text": "Good morning, John."},
        {"segment_id": "s2", "source_text": "Where is my sword?"},
    ]
    plan = build_block_plan(
        model_id="m",
        block_source=" ".join(s["source_text"] for s in segs),
        segments=segs,
        output_schema='{"translations": []}',
    )
    assert "[s1] Good morning, John." in plan.prompt
    assert "[s2] Where is my sword?" in plan.prompt
    assert "{{segment_batch_with_ids}}" not in plan.prompt
    assert 'Schema:\n[[none]]' not in plan.prompt
    assert '{"translations": []}' in plan.prompt
    # ids rendered exactly once, in request order
    assert plan.prompt.index("[s1]") < plan.prompt.index("[s2]")


def test_render_segment_batch_empty_and_missing_ids():
    assert render_segment_batch([]) == ""
    assert render_segment_batch([{"source_text": "Only text."}]) == "Only text."


# --- 2. small-model schema deviations are canonicalised ---------------------
def test_normalize_translation_alias():
    resp = {"translations": [{"segment_id": "s1", "translation": "Ciao."}]}
    out = _normalize_model_response(resp)
    assert out["translations"][0]["target_text"] == "Ciao."
    assert out["translations"][0]["segment_id"] == "s1"


def test_normalize_bare_list_and_other_aliases():
    out = _normalize_model_response(
        [{"segment_id": "s1", "translated_text": "Ciao."}])
    assert out["translations"][0]["target_text"] == "Ciao."

    out2 = _normalize_model_response(
        {"translations": [{"segment_id": "s1", "text": "Ciao."}]})
    assert out2["translations"][0]["target_text"] == "Ciao."


def test_normalize_keeps_canonical_response_untouched():
    resp = {"translations": [{"segment_id": "s1", "target_text": "Ciao.",
                              "flags": []}]}
    assert _normalize_model_response(resp) is resp  # no copy, no rewrite


# --- 3. _save_segments updates the existing (UUID-pk) row -------------------
def test_save_segments_updates_existing_uuid_row():
    """Regression: payload ids are str while TranslationUnit.id is a UUID.

    The old code built the ``existing`` dict with raw UUID keys, the str
    lookup silently missed, and the INSERT violated translation_units_pkey.
    The dict must be keyed by ``str(u.id)`` so the existing row is UPDATED.
    """
    from types import SimpleNamespace

    from backend.translation import runner as runner_mod

    sid = str(uuid.uuid4())
    existing_row = SimpleNamespace(
        id=uuid.UUID(sid), status="untranslated", target_text=None,
        model_run_id=None, ordinal=0, source_text="Hello.",
        source_hash="", source_flags={}, chapter_id=None)
    added = []

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return [existing_row]

        def scalar(self):
            return None  # max(numero) su progetto vuoto

    class _DB:
        def query(self, *_a, **_k):
            return _Q()

        def execute(self, *_a, **_k):
            pass  # lock di riga sul progetto: no-op col mock

        def add(self, obj):
            added.append(obj)

        def commit(self):
            pass

    saved = runner_mod._save_segments(
        _DB(), "p1", None,
        [{"segment_id": sid, "source_text": "Hello."}],
        [{"segment_id": sid, "target_text": "Ciao."}],
        model_run_id="run-1")
    assert saved == 1
    # the EXISTING row was re-added for its UPDATE; no NEW unit was built
    assert added == [existing_row]
    assert existing_row.target_text == "Ciao."
    assert existing_row.status == "machine_draft"
