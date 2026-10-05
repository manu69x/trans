"""Pure entity import/export (PRD §6.6, §12.3, §15.2, §15.4).

This module is *pure*: it contains no DB access so it can be unit-tested
without a database. Two responsibilities:

* :func:`import_csv` / :func:`export_csv` -- CSV import with a per-row
  error report and CSV export of the §6.4/§6.6 entity fields.
* :func:`import_tbx` / :func:`export_tbx` -- TBX (TEI Simple Bilingual
  Glossary) import/export for the same fields.

The entity record is a superset of the glossary term: it adds the
referential/grammatical-gender, number, policy, aliases, forbidden
targets, priority and the two §6.6 flags. All values are validated
against the same allowed sets the API layer uses (so a bad value fails
at import and at the endpoint).
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable, Mapping

# --- allowed value sets (mirror the API layer) ------------------------------
ENTITY_STATUSES: frozenset[str] = frozenset(
    {"proposed", "verified", "approved", "deprecated", "merged", "archived"}
)
REF_GENDERS: frozenset[str] = frozenset(
    {"male", "female", "nonbinary", "mixed", "unknown", "not_applicable"}
)
IT_GENDERS: frozenset[str] = frozenset(
    {"masculine", "feminine", "common", "variable", "not_applicable"}
)
IT_NUMBERS: frozenset[str] = frozenset(
    {"singular", "plural", "invariant", "collective", "unknown"}
)
ENTITY_TYPES: frozenset[str] = frozenset(
    {
        "PERSON", "ROLE", "CREATURE_SPECIES", "OBJECT_ARTIFACT",
        "LOCATION", "ORG_FACTION", "WORK_MEDIA", "EVENT",
        "CONCEPT_TERM", "TITLE_HONORIFIC",
    }
)
POLICIES: frozenset[str] = frozenset(
    {"keep_source", "translate", "transliterate", "contextual", "undecided"}
)
PRIORITIES: frozenset[str] = frozenset({"block_batch", "warn", "normal"})

# CSV header, in export order.
CSV_COLUMNS: tuple[str, ...] = (
    "canonical_source",
    "canonical_target",
    "entity_type",
    "referential_gender",
    "referential_gender_evidence",
    "italian_grammatical_gender",
    "grammatical_number",
    "translation_policy",
    "definition",
    "notes",
    "status",
    "confidence",
    "aliases",
    "forbidden_targets",
    "priority",
    "never_translate",
    "allow_inflection",
)

# Pipe-separated list columns (aliases, forbidden_targets) can't use the
# CSV comma.
_LIST_SEP = "|"

_TRUE = "true"
_FALSE = "false"


# --- validation helpers -----------------------------------------------------
def _clean(value: Any) -> str:
    """Normalise an incoming value to a string; ``None`` becomes ``""``."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _LIST_SEP.join(str(v) for v in value)
    return str(value)


def _as_list(value: Any) -> list[str]:
    """Parse a pipe-separated (CSV) or JSON (TBX2) list into a Python list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    items = [p.strip() for p in str(value).split(_LIST_SEP)]
    return [p for p in items if p]


def _as_bool(value: Any) -> bool:
    """Interpret a CSV/JSON value as a boolean (default False on garbage)."""
    if isinstance(value, bool):
        return value
    text = _clean(value).strip().lower()
    if text in {"1", "true", "yes", "y", "t", "preferred", "preferito", "sì"}:
        return True
    if text in {"0", "false", "no", "n", "f", "", "not", "vetato", "vietato", "no"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _as_float(value: Any) -> float | None:
    text = _clean(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"invalid confidence: {value!r}") from exc


def _validate_entity_type(value: Any) -> str:
    text = _clean(value).strip().upper()
    if not text:
        raise ValueError("entity_type is required")
    if text not in ENTITY_TYPES:
        raise ValueError(
            f"unknown entity_type {text!r}; allowed: "
            f"{', '.join(sorted(ENTITY_TYPES))}"
        )
    return text


def _validate_status(value: Any) -> str:
    text = _clean(value).strip().lower()
    if not text:
        raise ValueError("status is required")
    if text not in ENTITY_STATUSES:
        raise ValueError(
            f"unknown status {text!r}; allowed: "
            f"{', '.join(sorted(ENTITY_STATUSES))}"
        )
    return text


def _validate_ref_gender(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in REF_GENDERS:
        raise ValueError(
            f"invalid referential_gender {text!r}; allowed: "
            f"{', '.join(sorted(REF_GENDERS))}"
        )
    return text


def _validate_it_gender(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in IT_GENDERS:
        raise ValueError(
            f"invalid IT grammatical_gender {text!r}; allowed: "
            f"{', '.join(sorted(IT_GENDERS))}"
        )
    return text


def _validate_number(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in IT_NUMBERS:
        raise ValueError(
            f"invalid grammatical_number {text!r}; allowed: "
            f"{', '.join(sorted(IT_NUMBERS))}"
        )
    return text


def _validate_policy(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in POLICIES:
        raise ValueError(
            f"invalid translation_policy {text!r}; allowed: "
            f"{', '.join(sorted(POLICIES))}"
        )
    return text


def _validate_priority(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in PRIORITIES:
        raise ValueError(
            f"invalid priority {text!r}; allowed: "
            f"{', '.join(sorted(PRIORITIES))}"
        )
    return text


def _clean_entity(row: Mapping[str, Any], row_no: int) -> dict:
    """Validate one incoming row and return a normalised entity dict.

    Raises ``ValueError`` with a human-readable message on the first
    problem so the caller can build a row-level error report.
    """
    source = _clean(row.get("canonical_source")).strip()
    if not source:
        raise ValueError(f"row {row_no}: canonical_source is required")

    target = _clean(row.get("canonical_target")).strip() or None
    entity_type = _validate_entity_type(row.get("entity_type"))
    status = _validate_status(row.get("status"))

    ref_gender = _validate_ref_gender(row.get("referential_gender"))
    ref_evidence = (
        _clean(row.get("referential_gender_evidence")).strip() or None
    )
    it_gender = _validate_it_gender(row.get("italian_grammatical_gender"))
    number = _validate_number(row.get("grammatical_number"))
    policy = _validate_policy(row.get("translation_policy"))

    definition = _clean(row.get("definition")).strip() or None
    notes = _clean(row.get("notes")).strip() or None
    confidence = _as_float(row.get("confidence"))

    aliases = _as_list(row.get("aliases"))
    forbidden = _as_list(row.get("forbidden_targets"))
    priority = _validate_priority(row.get("priority"))
    never = _as_bool(row.get("never_translate", False))
    allow_inf = _as_bool(row.get("allow_inflection", True))

    return {
        "canonical_source": source,
        "canonical_target": target,
        "entity_type": entity_type,
        "status": status,
        "referential_gender": ref_gender,
        "referential_gender_evidence": ref_evidence,
        "italian_grammatical_gender": it_gender,
        "grammatical_number": number,
        "translation_policy": policy,
        "definition": definition,
        "notes": notes,
        "confidence": confidence,
        "aliases": aliases,
        "forbidden_targets": forbidden,
        "priority": priority,
        "never_translate": never,
        "allow_inflection": allow_inf,
    }


# --- CSV --------------------------------------------------------------------
def export_csv(entities: Iterable[Mapping[str, Any]]) -> str:
    """Serialise an iterable of entity-like mappings to CSV text."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for e in entities:
        writer.writerow([
            _clean(e.get("canonical_source")),
            _clean(e.get("canonical_target")),
            _clean(e.get("entity_type")),
            _clean(e.get("referential_gender")),
            _clean(e.get("referential_gender_evidence")),
            _clean(e.get("italian_grammatical_gender")),
            _clean(e.get("grammatical_number")),
            _clean(e.get("translation_policy")),
            _clean(e.get("definition")),
            _clean(e.get("notes")),
            _clean(e.get("status")),
            _clean(e.get("confidence")),
            _LIST_SEP.join(e.get("aliases") or []),
            _LIST_SEP.join(e.get("forbidden_targets") or []),
            _clean(e.get("priority")),
            _TRUE if e.get("never_translate") else _FALSE,
            _TRUE if e.get("allow_inflection", True) else _FALSE,
        ])
    return buf.getvalue()


def import_csv(text: str) -> tuple[list[dict], list[dict]]:
    """Parse CSV text into ``(entities, errors)``.

    ``entities`` is the list of validated, normalised entity dicts ready to
    be inserted; ``errors`` is a per-failed-row report of
    ``{"row": N, "error": "..."}``. A row that fails validation is skipped;
    valid rows are still returned so the caller can import the good ones
    even when some rows are bad (§12.3 partial-import contract).
    """
    reader = csv.DictReader(io.StringIO(text))
    entities: list[dict] = []
    errors: list[dict] = []
    for i, raw in enumerate(reader, start=1):
        # DictReader keeps unknown columns under None; drop empties.
        row = {k: v for k, v in raw.items() if k is not None and v is not None}
        try:
            entities.append(_clean_entity(row, i))
        except ValueError as exc:
            errors.append({"row": i, "error": str(exc)})
    return entities, errors


# --- TBX (TEI Simple Bilingual Glossary) ------------------------------------
def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def export_tbx(entities: Iterable[Mapping[str, Any]]) -> str:
    """Serialise entity-like mappings to a TBX2 bilingual glossary (TEI).

    Each entry carries the English source and, on the ``<entry>`` element,
    the metadata the PRD §6.4/§6.5/§6.6 require (type, status, referential
    and IT gender, number, policy, aliases, forbidden targets, priority).
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TEI xmlns="http://www.tei-c.org/ns/1.0">',
        "  <teiHeader>",
        "    <fileDesc>",
        "      <head>Trans entity export (TBX2)</head>",
        "    </fileDesc>",
        "  </teiHeader>",
        "  <text>",
        "    <group>",
    ]
    for e in entities:
        entry_id = f"e{len(parts)}"
        source = _xml_escape(_clean(e.get("canonical_source")) or "")
        target = _xml_escape(_clean(e.get("canonical_target")) or "")
        entity_type = _xml_escape(_clean(e.get("entity_type")) or "")
        status = _xml_escape(_clean(e.get("status")) or "proposed")
        ref_gender = _clean(e.get("referential_gender") or "")
        it_gender = _clean(e.get("italian_grammatical_gender") or "")
        number = _clean(e.get("grammatical_number") or "")
        policy = _clean(e.get("translation_policy") or "")
        priority = _clean(e.get("priority") or "")
        aliases = e.get("aliases") or []
        forbidden = e.get("forbidden_targets") or []

        parts.append("      <entry")
        parts.append(
            f'          type="{entity_type}" status="{status}"'
        )
        if ref_gender:
            parts.append(f'          ref_gender="{_xml_escape(ref_gender)}"')
        if it_gender:
            parts.append(
                f'          gender_it="{_xml_escape(it_gender)}"')
        if number:
            parts.append(f'          number_it="{_xml_escape(number)}"')
        if policy:
            parts.append(f'          policy="{_xml_escape(policy)}"')
        if priority:
            parts.append(f'          priority="{_xml_escape(priority)}"')
        parts.append("        >")
        parts.append("          <head>")
        if target:
            parts.append(
                f'            <gloss><ref target="#{entry_id}">{target}</ref></gloss>'
            )
        parts.append("          </head>")
        parts.append('          <glossgrp lang="en">')
        parts.append("            <lg>")
        parts.append(f"              <l>{source}</l>")
        parts.append("            </lg>")
        parts.append("          </glossgrp>")
        notes: list[str] = []
        if ref_gender:
            notes.append(f'            <note type="ref_gender">{ref_gender}</note>')
        for a in aliases:
            notes.append(
                f'            <note type="alias">{_xml_escape(a)}</note>')
        for f in forbidden:
            notes.append(
                f'            <note type="forbidden">{_xml_escape(f)}</note>')
        if notes:
            parts.append("          <desc>")
            parts.extend(notes)
            parts.append("          </desc>")
        parts.append("        </entry>")
    parts.extend([
        "    </group>",
        "  </text>",
        "</TEI>",
    ])
    return "\n".join(parts) + "\n"


def import_tbx(text: str) -> tuple[list[dict], list[dict]]:
    """Parse a TBX2 glossary into ``(entities, errors)``.

    Uses :mod:`xml.etree.ElementTree`; namespace prefixes are stripped so
    the reader is tolerant of both namespaced and bare elements.
    """
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def _by_desc(parent: "ET.Element", name: str) -> list:
        return [c for c in parent if _local(c.tag) == name]

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [], [{"row": 0, "error": f"invalid XML: {exc}"}]

    entities: list[dict] = []
    errors: list[dict] = []

    def _by(name: str) -> list:
        return [e for e in root.iter() if _local(e.tag) == name]

    for i, entry in enumerate(_by("entry"), start=1):
        try:
            source = None
            target = None
            for grp in _by_desc(entry, "glossgrp"):
                lang = (grp.get("lang") or "").lower()
                ls = [c for c in grp.iter() if _local(c.tag) == "l"]
                gloss = " ".join(_l_text(l) for l in ls)
                if lang == "it":
                    target = gloss.strip()
                elif lang in ("en", "eng"):
                    source = gloss.strip()
            if source is None:
                for head in _by_desc(entry, "head"):
                    for g in _by_desc(head, "gloss"):
                        ref = _by_desc(g, "ref")
                        if ref and ref[0].text:
                            source = ref[0].text.strip()
            if not source:
                raise ValueError(f"row {i}: missing English source gloss")

            def _attr(name: str) -> str:
                v = entry.get(_local(name))
                return v.strip() if v else ""

            aliases: list[str] = []
            forbidden: list[str] = []
            for desc in _by_desc(entry, "desc"):
                for note in _by_desc(desc, "note"):
                    ntype = (note.get("type") or "").lower()
                    if ntype == "alias" and note.text:
                        aliases.append(note.text)
                    elif ntype == "forbidden" and note.text:
                        forbidden.append(note.text)

            entity = {
                "canonical_source": source,
                "canonical_target": target,
                "entity_type": _attr("type") or "CONCEPT_TERM",
                "status": _attr("status") or "proposed",
                "referential_gender": _attr("ref_gender") or None,
                "italian_grammatical_gender": _attr("gender_it") or None,
                "grammatical_number": _attr("number_it") or None,
                "translation_policy": _attr("policy") or None,
                "priority": _attr("priority") or None,
                "aliases": aliases,
                "forbidden_targets": forbidden,
            }
            entities.append(_clean_entity(entity, i))
        except ValueError as exc:
            errors.append({"row": i, "error": str(exc)})
    return entities, errors


def _l_text(l: "ET.Element") -> str:
    """Return the readable text of a TBX ``<l`` gloss line, dropping refs.

    Uses :meth:`itertext` so text inside a ``<ref``> child (as the export
    produces) is still recovered.
    """
    return "".join(l.itertext())
