"""Test del testo ridotto passato a QE: prima + ultima frase (2026-09-23)."""
from backend.verify_handler import head_tail_sentences


def test_head_tail_multi_sentence():
    assert head_tail_sentences(
        "Prima frase. Seconda frase. Ultima frase") == \
        "Prima frase. Ultima frase"


def test_head_tail_keeps_final_period():
    assert head_tail_sentences("Una. Due. Tre.") == "Una. Tre."


def test_single_sentence_whole_segment():
    t = "Una sola frase senza punto finale"
    assert head_tail_sentences(t) == t


def test_no_period_whole_segment():
    t = "testo senza delimitatori"
    assert head_tail_sentences(t) == t


def test_empty_and_none():
    assert head_tail_sentences("") == ""
    assert head_tail_sentences(None) == ""


def test_two_sentences():
    assert head_tail_sentences("Apertura. Chiusura") == "Apertura. Chiusura"
