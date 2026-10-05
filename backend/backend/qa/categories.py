"""QA taxonomy: MQM error categories and severities (PRD §10.3 / §10.4).

The categories follow the MQM schema in §10.4 (accuracy / terminology /
Italian / style / locale / source); the severities follow the §10.4 scale
``minor`` / ``major`` / ``critical``. They are shared by the deterministic
suite, the reference-free QE, the critic and the human annotator, so every
producer of issues (automatic ``kind=qa|critic|qe|ocr|entity`` or human
``kind=human``) uses one, validated vocabulary.
"""
from __future__ import annotations

# --- severity (§10.4) ------------------------------------------------------
MINOR = "minor"
MAJOR = "major"
CRITICAL = "critical"
SEVERITIES = (MINOR, MAJOR, CRITICAL)
# ordering used to rank issues (critical first)
SEVERITY_RANK = {MINOR: 0, MAJOR: 1, CRITICAL: 2}

# --- categories (§10.4) ----------------------------------------------------
# accuracy
MISTRANSLATION = "mistranslation"
OMISSION = "omission"
ADDITION = "addition"
UNTRANSLATED = "untranslated"
# terminology
WRONG_TERM = "wrong_term"
FORBIDDEN_TERM = "forbidden_term"
TERM_INCONSISTENCY = "term_inconsistency"
# Italian
GRAMMAR = "grammar"
SPELLING = "spelling"
PUNCTUATION = "punctuation"
COLLOCATION = "collocation"
# style
AWKWARD = "awkward"
REGISTER = "register"
VOICE = "voice"
TONE = "tone"
CALCO = "calco"
REPETITION = "repetition"
# locale / convenzioni
FORMATTING = "formatting"
QUOTES = "quotes"
UNITS_DATA = "units_data"
# source
ORC_SUSPECTED = "ocr_suspected"
AMBIGUOUS_SOURCE = "ambiguous_source"

# kind used by the ``kind`` column of ``qa_issues``.
KIND_QA = "qa"
KIND_CRITIC = "critic"
KIND_QE = "qe"
# a human revisor annotating a span (§10.4) — AC1.
KIND_HUMAN = "human"

#: The §10.4 group layout: every category belongs to exactly one group.
CATEGORY_GROUPS: dict[str, tuple[str, ...]] = {
    "accuracy": (
        MISTRANSLATION, OMISSION, ADDITION, UNTRANSLATED,
    ),
    "terminology": (
        WRONG_TERM, FORBIDDEN_TERM, TERM_INCONSISTENCY,
    ),
    "italian": (
        GRAMMAR, SPELLING, PUNCTUATION, COLLOCATION,
    ),
    "style": (
        AWKWARD, REGISTER, VOICE, TONE, CALCO, REPETITION,
    ),
    "locale": (
        FORMATTING, QUOTES, UNITS_DATA,
    ),
    "source": (
        ORC_SUSPECTED, AMBIGUOUS_SOURCE,
    ),
}

CATEGORIES: tuple[str, ...] = tuple(
    cat for cats in CATEGORY_GROUPS.values() for cat in cats)
