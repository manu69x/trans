"""Regressione: la validazione entità deve girare dentro validate_response.

Bug 2026-09-18: validate_response passava i payload §7.3 (lista di dict) a
_as_set, che li appiattiva in set() -> validate_entities usciva subito con
``if not entities`` e il controllo §10.2 non girava mai nel percorso
automatico (accettazione §15.3 solo cartacea).
"""
from __future__ import annotations

from backend.translation.validators import (
    SOFT,
    validate_response,
)


def _response(*pairs: tuple[str, str]) -> dict:
    return {"translations": [
        {"segment_id": sid, "target_text": txt} for sid, txt in pairs
    ]}


_CONSTRAINTS = {
    "forbidden_terms": ["damn"],
    "must_keep": {"Sherlock Holmes"},
    "entities": [
        {"canonical_source": "Watson", "canonical_target": "Watson",
         "policy": "not_translate"},
        {"canonical_source": "Baker Street", "canonical_target": "Baker Street",
         "policy": "translate"},
    ],
}


def test_entity_violation_is_reported():
    requested = [
        {"segment_id": "s1", "source_text": "Watson closed the door."},
        {"segment_id": "s2", "source_text": "He walked to Baker Street."},
    ]
    response = _response(
        ("s1", "Uatson chiuse la porta."),
        ("s2", "Camminò fino a Via Baker."),
    )
    errors = validate_response(requested=requested, response=response,
                               constraints=_CONSTRAINTS)
    entity_errors = [e for e in errors if e["kind"] == "entity"]
    assert entity_errors, "entity violations must not be silently skipped"
    assert all(e["severity"] == SOFT for e in entity_errors)
    sids = {e["segment_id"] for e in entity_errors}
    assert sids == {"s1", "s2"}


def test_compliant_entities_pass():
    requested = [
        {"segment_id": "s1", "source_text": "Watson closed the door."},
        {"segment_id": "s2", "source_text": "He walked to Baker Street."},
    ]
    response = _response(
        ("s1", "Watson chiuse la porta."),
        ("s2", "Camminò fino a Baker Street."),
    )
    errors = validate_response(requested=requested, response=response,
                               constraints=_CONSTRAINTS)
    assert not [e for e in errors if e["kind"] == "entity"]


def test_entities_not_mentioned_in_source_are_ignored():
    requested = [
        {"segment_id": "s1", "source_text": "The fog covered the city."},
    ]
    response = _response(("s1", "La nebbia copriva la città."))
    errors = validate_response(requested=requested, response=response,
                               constraints=_CONSTRAINTS)
    assert not [e for e in errors if e["kind"] == "entity"]


def test_string_constraints_still_enforced():
    requested = [
        {"segment_id": "s1", "source_text": "It was a damn cold night."},
    ]
    response = _response(("s1", "Che damn, che notte fredda. "
                                "Sherlock Holmes attese."))
    errors = validate_response(requested=requested, response=response,
                               constraints=_CONSTRAINTS)
    kinds = {e["kind"] for e in errors}
    assert "forbidden_term" in kinds
    assert "must_keep" not in kinds


# --- flessione italiana (flag allow_inflection, fix 2026-09-19) ------------

_INFLECT = [
    {"canonical_source": "casa", "canonical_target": "casa",
     "policy": "translate", "allow_inflection": True},
]


def test_inflection_allows_plural_of_canonical_target():
    requested = [
        {"segment_id": "s1", "source_text": "The books were everywhere."},
    ]
    response = _response(("s1", "I libri erano dappertutto."))
    errors = validate_response(requested=requested, response=response,
                               constraints={
                                   "entities": [
                                       {"canonical_source": "books",
                                        "canonical_target": "libro",
                                        "policy": "translate",
                                        "allow_inflection": True}]})
    assert not [e for e in errors if e["kind"] == "entity"]


def test_no_inflection_requires_exact_canonical_target():
    requested = [
        {"segment_id": "s1", "source_text": "The books were everywhere."},
    ]
    response = _response(("s1", "I libri erano dappertutto."))
    errors = validate_response(requested=requested, response=response,
                               constraints={
                                   "entities": [
                                       {"canonical_source": "books",
                                        "canonical_target": "libro",
                                        "policy": "translate",
                                        "allow_inflection": False}]})
    assert any(e["kind"] == "entity" for e in errors)


def test_not_translate_with_inflection_accepts_derived_form():
    requested = [
        {"segment_id": "s1", "source_text": "They reached the camp."},
    ]
    response = _response(("s1", "Arrivarono al campo-base."))
    errors = validate_response(requested=requested, response=response,
                               constraints={
                                   "entities": [
                                       {"canonical_source": "camp",
                                        "canonical_target": "campo",
                                        "policy": "not_translate",
                                        "allow_inflection": True}]})
    assert not [e for e in errors if e["kind"] == "entity"]


def test_not_translate_without_inflection_stays_strict():
    requested = [
        {"segment_id": "s1", "source_text": "They reached the camp."},
    ]
    response = _response(("s1", "Arrivarono al campeggio."))
    errors = validate_response(requested=requested, response=response,
                               constraints={
                                   "entities": [
                                       {"canonical_source": "camp",
                                        "canonical_target": "campo",
                                        "policy": "not_translate",
                                        "allow_inflection": False}]})
    assert any(e["kind"] == "entity" for e in errors)
