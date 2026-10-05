"""Parse tollerante dell'output modello (fix 2026-09-21: caratteri di controllo)."""
from backend.gateway_http import _parse_model_json


def test_plain_json():
    assert _parse_model_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    assert _parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_control_characters_inside_strings():
    # i modelli locali emettono a volte newline reali dentro i valori stringa
    raw = '{"translations": [{"segment_id": "s1",\n "target_text": "riga1\nriga2"}]}'
    parsed = _parse_model_json(raw)
    assert parsed["translations"][0]["target_text"] == "riga1\nriga2"
