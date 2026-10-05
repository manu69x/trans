"""Rendering delle voci di glossario/entità nel prompt §9.4.

Le righe devono portare policy, genere/numero grammaticali italiani (per
l'accordo con l'articolo) e termini vietati.
"""
from __future__ import annotations

from backend.translation.planner import render_glossary_section


def test_not_translate_with_gender_and_number():
    out = render_glossary_section(
        [{"source": "Warson", "target": "Warson", "policy": "not_translate",
          "gender": "masculine", "number": "singular"}], [])
    assert out == ("- Warson -> Warson  "
                   "[NON TRADURRE: usa questa forma · maschile singolare]")


def test_translate_policy_with_forbidden():
    out = render_glossary_section(
        [{"source": "Fraternité", "target": "Fraternità",
          "policy": "translate",
          "forbidden_targets": ["Fraternanza", "Confraternita"]}], [])
    assert out == ("- Fraternité -> Fraternità  "
                   "[VIETATI: Fraternanza, Confraternita]")


def test_gender_not_applicable_is_omitted():
    out = render_glossary_section(
        [{"source": "London", "target": "Londra", "policy": "translate",
          "gender": "not_applicable", "number": "unknown"}], [])
    assert out == "- London -> Londra"


def test_entity_payload_keys_are_accepted():
    # il planner route usa le chiavi italian_grammatical_gender/number
    out = render_glossary_section(
        [], [{"canonical_source": "Mary", "canonical_target": "Mary",
              "policy": "not_translate",
              "italian_grammatical_gender": "feminine",
              "grammatical_number": "singular"}])
    assert out == ("- Mary -> Mary  "
                   "[NON TRADURRE: usa questa forma · femminile singolare]")


def test_empty_sections_render_empty():
    assert render_glossary_section([], []) == ""
