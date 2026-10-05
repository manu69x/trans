"""Tests for the EN preprocessing pipeline.

Acceptance criteria under test (PRD §5.5, §15.2):

* AC1 — Saxon genitive: a possessive ``'s`` on a proper noun/NP is
  normalised to the reversible control token ``[[POSS:Mary]] 's`` while
  ``Mary's late`` (is), ``Mary's been`` (has) and ``Let's`` (us) stay
  untouched (§5.5 table, §15.2).
* AC2 — the isolated pronoun ``I`` (PRP) never appears among entity or
  termbase candidates, while ``Icarus``/``Unit I``/``I-5`` stay valid;
  the hard rule is enforced both in the pure filter and in the entity
  clustering (§5.5, §15.2).
* AC3 — the transformation is reversible without loss: ``restore()``
  reconstructs the original text exactly and no ``[[...]]`` control token
  survives the restore (§5.5.1, §5.5.5).
"""
from __future__ import annotations

import pytest

from backend.parsing import preprocess as pp


# --------------------------------------------------------------------------
# AC1 — Saxon genitive (§5.5 table, §15.2)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("source,expected", [
    ("Mary's coat", "[[POSS:Mary]] 's coat"),
    ("John's book", "[[POSS:John]] 's book"),
    ("Mr. Dashwood's estate", "[[POSS:Mr. Dashwood]] 's estate"),
    # the §5.5 table: is / has / us readings stay untouched
    ("Mary's late", "Mary's late"),
    ("Mary's been here", "Mary's been here"),
    ("Let's go", "Let's go"),
    ("It's fine", "It's fine"),
])
def test_ac1_possessive_table(source, expected):
    assert pp.preprocess(source).normalized == expected


def test_ac1_control_token_shape_and_map():
    """The control token is glued to the owner (no visible space) and the
    transform map records the exact original span (§5.5.1/§5.5.3)."""
    res = pp.preprocess("Mary's coat")
    assert "[[POSS:Mary]] 's" in res.normalized
    poss = [t for t in res.transforms if t.kind == "possessive"]
    assert len(poss) == 1
    t = poss[0]
    assert t.original == "Mary's"
    assert t.replacement == "[[POSS:Mary]] 's"
    assert res.original[t.start:t.end] == "Mary's"
    assert t.meta["owner"] == "Mary"


def test_ac1_curly_apostrophe_genitive():
    """The typographic apostrophe is normalised before the parse and the
    genitive still matches (§5.5.4 controlled quote normalisation)."""
    res = pp.preprocess("John’s book")
    assert res.normalized == "[[POSS:John]] 's book"


def test_ac1_non_possessive_kept_even_in_whitespace_variants():
    """A PRP 's (verb contraction) must never be marked, however the
    sentence continues (regression for the copula reading)."""
    for sentence in ("Mary's late again", "Mary's late.", "Tom's here"):
        assert pp.preprocess(sentence).normalized == sentence


# --------------------------------------------------------------------------
# AC2 — isolated pronoun I (§5.5, §15.2)
# --------------------------------------------------------------------------
def test_ac2_isolated_I_never_a_candidate():
    kept, dropped = pp.filter_I_candidates([
        {"text": "I", "pos": "PRP"},
        {"text": "I"},  # conservative surface test (no POS)
        {"text": "Icarus", "pos": "PROPN"},
        {"text": "Unit I"},
        {"text": "I-5"},
    ])
    assert [c["text"] for c in dropped] == ["I", "I"]
    assert [c["text"] for c in kept] == ["Icarus", "Unit I", "I-5"]


def test_ac2_I_not_lowercase_or_multichar():
    """The rule is exact-token: 'i', 'It', 'ICE' are other words."""
    kept, dropped = pp.filter_I_candidates(
        [{"text": t} for t in ("i", "It", "ICE")])
    assert dropped == []
    assert len(kept) == 3


def test_ac2_entity_clusters_drop_isolated_I():
    """Belt-and-braces inside the entity pipeline: a BookNLP cluster whose
    canonical surface is the isolated ``I`` never reaches the proposals
    (§15.2)."""
    from backend.parsing import entities as ents

    page_map = [(0, 7, 1)]
    raw = {
        "tokens": [],
        "entities": [
            {"coref": "0", "start": 0, "end": 0, "prop": "PRON",
             "cat": "PER", "text": "I"},
            {"coref": "1", "start": 0, "end": 0, "prop": "PROP",
             "cat": "PER", "text": "Kate"},
        ],
        "book": {"characters": []},
    }
    clusters, _ = ents.cluster_mentions(raw, page_map, None, 1)
    surfaces = [c.canonical_source for c in clusters.values()]
    assert "I" not in surfaces, surfaces
    assert "Kate" in surfaces, surfaces


def test_ac2_isolated_I_through_spacy_prp():
    """End-to-end hard rule: the exact token I parsed as PRP is reported
    as an isolated first-person pronoun; Unit I is not."""
    import spacy

    doc = spacy.load("en_core_web_sm")("I said Unit I survived.")
    assert pp.is_isolated_I(doc[0].text, doc[0].tag_) is True   # PRP
    assert pp.is_isolated_I(doc[4].text, doc[4].tag_) is False  # not PRP
    assert pp.is_isolated_I("Icarus") is False


# --------------------------------------------------------------------------
# AC3 — reversible roundtrip, no residue (§5.5.1, §5.5.5)
# --------------------------------------------------------------------------
ROUNDTRIP_SAMPLES = [
    "Mary's coat",
    "John's book, Mary's late; Let's go — it's 12:30.",
    "John’s book, Mary’s late; Let’s go — it’s 12:30.",
    "In 1897 the ship sailed (see http://example.com/a?b=1). "
    "Write to a.b@test.it. Unit I survived.",
    "The “curly quotes” and NBSP\u00a0stay; 1,000 men saw it on 12/06/1805.",
    "Elinor's sister married Mr. Dashwood. She was 21.",
]


@pytest.mark.parametrize("source", ROUNDTRIP_SAMPLES)
def test_ac3_roundtrip_lossless(source):
    res = pp.preprocess(source)
    assert pp.restore(res.normalized, res.transforms) == source


@pytest.mark.parametrize("source", ROUNDTRIP_SAMPLES)
def test_ac3_roundtrip_through_json(source):
    """The map survives serialisation (persisted next to the segment)."""
    res = pp.PreprocessResult.from_dict(pp.preprocess(source).to_dict())
    assert pp.restore(res.normalized, res.transforms) == source


@pytest.mark.parametrize("source", ROUNDTRIP_SAMPLES)
def test_ac3_no_control_tokens_in_final_text(source):
    """After post-translation stripping (T22 hook) no [[...]] remains."""
    res = pp.preprocess(source)
    cleaned, removed = pp.strip_control_tokens(res.normalized)
    assert pp.validate_no_control_tokens(cleaned) == []
    if "[[POSS:" in res.normalized:
        assert removed, "the owners must be reported for QA"


def test_ac3_restore_survives_llm_with_translated_prose():
    """An LLM keeps the control token + placeholders but rewrites the rest:
    restore still returns every protected literal (§5.5.4/§5.5.5)."""
    res = pp.preprocess("Mary's coat cost 3.50 at http://x.it")
    translated = ("[[POSS:Mary]] 's coat — ⟦NUMBER1⟧ — ⟦URL1⟧. "
                  "Il cappotto di Mary.")
    out = pp.restore(translated, res.transforms)
    assert "Mary's coat" in out
    assert "3.50" in out
    assert "http://x.it" in out
    assert pp.validate_no_control_tokens(out) == []


def test_ac3_nfc_recorded():
    res = pp.preprocess("e\u0301")  # decomposed e-acute
    assert res.normalized == "é"
    assert any(t.kind == "unicode" and t.meta.get("form") == "NFC"
               for t in res.transforms)


def test_ac3_nbsp_and_layout_preserved():
    text = "A\u00a0B\nC"
    res = pp.preprocess(text)
    assert "\u00a0" in res.normalized and "\n" in res.normalized
    assert pp.restore(res.normalized, res.transforms) == text
