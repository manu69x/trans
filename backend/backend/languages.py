"""Catalogo lingue del progetto (multilingua Tier 1, 2026-10-29).

La source resta sempre ``en`` (l'estrazione entità BookNLP upstream è
inglese-only); i target Tier 1 sono le grandi lingue di editoria tradotta
dall'inglese, tutte nel set "core" di TranslateGemma (55 lingue, model card
ufficiale): it, es, fr, de, pt.

Per ogni lingua target il catalogo porta:
* ``name_en`` / ``name_it``  -- nome sostantivo (prompt e QE);
* ``adjective_it``           -- aggettivo italiano ("letteraria spagnola");
* ``word_markers`` / ``extra_markers`` / ``diacritics`` -- prefiltri
  anti-copia del runner (segno che il target è davvero nella lingua);
* ``grammar_labels``         -- genere/numero nel prompt del glossario.

La coppia EN→IT deve produrre prompt byte-per-byte equivalenti al vecchio
skeleton cablato (zero regressione sul flusso esistente).
"""
from __future__ import annotations

import re

SUPPORTED_SOURCE = "en"
SUPPORTED_TARGETS = ("it", "es", "fr", "de", "pt")

LANGUAGES: dict[str, dict] = {
    "en": {
        "name_en": "English", "name_it": "inglese", "adjective_it": "inglese",
    },
    "it": {
        "name_en": "Italian", "name_it": "italiano", "adjective_it": "italiana",
        "word_markers": (
            r"\b(che|non|per|con|una|uno|il|la|le|gli|del|della|dello|degli|"
            r"delle|questo|questa|quello|quella|sono|essere|stato|stata|stati|"
            r"state|aveva|avevo|avevano|nella|nello|nelli|alla|dalla|sulla|"
            r"piu|più|gia|già|cosi|così|anche|loro|noi|voi|ma|come|dove|quando|"
            r"se|si|ha|hanno|ho|hai|sua|suo|miei|tuoi|suoi|questi|queste|"
            r"quelli|quelle|molto|troppo|poi|invece|davvero|forse)\b"
        ),
        "extra_markers": (r"\bi\b",),
        "diacritics": r"[àèéìíòóù]",
        "grammar_labels": {
            "masculine": "maschile", "feminine": "femminile",
            "singular": "singolare", "plural": "plurale",
        },
    },
    "es": {
        "name_en": "Spanish", "name_it": "spagnolo", "adjective_it": "spagnola",
        "word_markers": (
            r"\b(el|los|las|que|del|un|una|unos|unas|es|son|estaba|estaban|"
            r"pero|para|por|como|cuando|donde|este|esta|esto|ese|esa|eso|"
            r"su|sus|muy|mas|más|tambien|también|ya|se|ha|han|he|era|fue|"
            r"ser|estar|todo|todos|toda|todas|cada|entre|sobre|tras|desde|"
            r"hasta|segun|según|sin|mismo|misma|otros|otras|otro|otra|"
            r"nosotros|ustedes|ellos|ellas|hay|esta|está|con|ni|lo|al|"
            r"cuando|mientras|entonces|asi|así)\b"
        ),
        "extra_markers": (),
        "diacritics": r"[áéíóúñü¿¡]",
        "grammar_labels": {
            "masculine": "masculino", "feminine": "femenino",
            "singular": "singular", "plural": "plural",
        },
    },
    "fr": {
        "name_en": "French", "name_it": "francese", "adjective_it": "francese",
        "word_markers": (
            r"\b(les|des|du|une|est|sont|etait|était|que|qui|mais|avec|pour|"
            r"par|comme|quand|ce|cet|cette|ces|son|sa|ses|leur|leurs|tres|"
            r"très|plus|aussi|deja|déjà|dans|sur|sous|sans|meme|même|tout|"
            r"tous|toute|toutes|chaque|entre|elle|elles|ils|nous|vous|je|"
            r"ne|pas|etre|être|avoir|ont|c'est|qu'il|qu'elle|d'un|d'une|"
            r"aux|au|alors|apres|après)\b"
        ),
        "extra_markers": (),
        "diacritics": r"[àâçéèêëîïôùûüÿœ]",
        "grammar_labels": {
            "masculine": "masculin", "feminine": "féminin",
            "singular": "singulier", "plural": "pluriel",
        },
    },
    "de": {
        "name_en": "German", "name_it": "tedesco", "adjective_it": "tedesca",
        "word_markers": (
            r"\b(der|die|das|den|dem|des|ein|eine|einen|einem|einer|und|"
            r"ist|sind|war|waren|dass|nicht|mit|für|auf|von|zu|zum|zur|wie|"
            r"wann|dieser|diese|dieses|sein|seine|ihre|sehr|mehr|auch|"
            r"schon|wenn|sich|hat|haben|hatte|aber|oder|doch|nur|noch|"
            r"alle|jeder|jede|zwischen|unter|nach|vor|beim|durch|ohne|"
            r"gegen|kein|keine|man|war|wird|werden|wurde|kann|muss)\b"
        ),
        "extra_markers": (),
        "diacritics": r"[äöüß]",
        "grammar_labels": {
            "masculine": "Maskulin", "feminine": "Feminin", "neuter": "Neutrum",
            "singular": "Singular", "plural": "Plural",
        },
    },
    "pt": {
        "name_en": "Portuguese", "name_it": "portoghese",
        "adjective_it": "portoghese",
        "word_markers": (
            r"\b(os|as|um|uma|uns|umas|do|da|dos|das|não|nao|estava|mas|"
            r"para|por|como|quando|onde|este|esta|esse|essa|isso|seu|sua|"
            r"seus|suas|muito|muita|mais|tambem|também|ja|já|tem|têm|foi|"
            r"ser|estar|tudo|todos|toda|todas|cada|entre|sobre|sem|mesmo|"
            r"mesma|outros|outras|outro|outra|nos|vocês|eles|elas|com|"
            r"no|na|nos|nas|pelo|pela|entao|então|depois|quando)\b"
        ),
        "extra_markers": (),
        "diacritics": r"[ãõáéíóúâêôç]",
        "grammar_labels": {
            "masculine": "masculino", "feminine": "feminino",
            "singular": "singular", "plural": "plural",
        },
    },
}


def is_supported_pair(source_language: str, target_language: str) -> bool:
    """True per le coppie abilitate (Tier 1: en → {it,es,fr,de,pt})."""
    return (source_language == SUPPORTED_SOURCE
            and target_language in SUPPORTED_TARGETS)


def get_language(code: str) -> dict:
    """Dati della lingua; KeyError se non nel catalogo."""
    return LANGUAGES[code]


def language_name(code: str, locale: str = "en") -> str:
    """Nome sostantivo ("Spanish" / "spagnolo") per prompt e QE."""
    lang = get_language(code)
    return lang["name_it"] if locale == "it" else lang["name_en"]


def target_adjective_it(code: str) -> str:
    """Aggettivo italiano per il prompt ("letteraria spagnola")."""
    return get_language(code)["adjective_it"]


# --- prefiltri anti-copia per lingua target (usati dal runner) --------------

def target_marker_patterns(code: str) -> list[re.Pattern]:
    """Regex compilati che segnalano testo davvero nella lingua target.

    Il primo elemento è la lista parole frequenti (score), il secondo le
    regex aggiuntive (es. il pronome italiano ``i`` case-sensitive), l'ultimo
    i diacritici della lingua (presenza = segno certo).
    """
    lang = get_language(code)
    return [
        re.compile(lang.get("word_markers", r"(?!x)x"), re.IGNORECASE),
        *[re.compile(p) for p in lang.get("extra_markers", ())],
        re.compile(lang.get("diacritics", r"(?!x)x")),
    ]


def target_has_markers(text: str, target_lang: str) -> bool:
    """True se *text* mostra marcatori della lingua target (runner §anti-copy).

    Stessa semantica dell'euristica italiana originale: diacritici presenti
    → marker certo; altrimenti score (parole frequenti + extra) ≥ 2.
    """
    t = text or ""
    patterns = target_marker_patterns(target_lang)
    if patterns[-1].search(t):
        return True
    score = sum(len(p.findall(t)) for p in patterns[:-1])
    return score >= 2
