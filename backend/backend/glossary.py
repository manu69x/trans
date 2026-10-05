"""Termbase (glossary) import/export and §7.3 entity selection (PRD §6.5, §7.1, §7.3).

This module is *pure*: it contains no DB access so it can be unit-tested
without a database. Three responsibilities:

* :func:`import_csv` / :func:`export_csv` -- CSV import with a per-row
  error report and CSV export.
* :func:`import_tbx` / :func:`export_tbx` -- TBX (TEI Simple Bilingual
  Glossary) import/export.
* :func:`select_entities_for_block` -- the §7.3 selection: pick the
  entities mentioned in the block, in the two preceding segments, reached
  by coreference, plus high-priority terms with lexical overlap, capped at
  30 entities + 20 terms (both configurable).

All values are validated against the same allowed sets the API layer uses
(so a bad value fails at import and at the endpoint).
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable, Mapping

# --- allowed value sets (mirror the API layer) ------------------------------
TERM_STATUSES: frozenset[str] = frozenset(
    {"proposed", "verified", "approved", "deprecated", "archived"}
)
IT_GENDERS: frozenset[str] = frozenset(
    {"masculine", "feminine", "common", "variable", "not_applicable"}
)
IT_NUMBERS: frozenset[str] = frozenset(
    {"singular", "plural", "invariant", "collective", "unknown"}
)
# §6.3 entity categories, reused as the term_type vocabulary.
TERM_TYPES: frozenset[str] = frozenset(
    {
        "PERSON", "ROLE", "CREATURE_SPECIES", "OBJECT_ARTIFACT",
        "LOCATION", "ORG_FACTION", "WORK_MEDIA", "EVENT",
        "CONCEPT_TERM", "TITLE_HONORIFIC",
    }
)

# CSV header, in export order.
CSV_COLUMNS: tuple[str, ...] = (
    "source_term",
    "target_term",
    "term_type",
    "preferred",
    "forbidden_targets",
    "grammatical_gender_it",
    "grammatical_number",
    "inflection_notes",
    "usage_notes",
    "status",
)

# Pipe-separated list column (forbidden_targets) can't use the CSV comma.
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


def _as_bool(value: Any) -> bool:
    """Interpret a CSV/JSON value as a boolean (default False on garbage)."""
    if isinstance(value, bool):
        return value
    text = _clean(value).strip().lower()
    if text in {"1", "true", "yes", "y", "t", "preferred", "preferito"}:
        return True
    if text in {"0", "false", "no", "n", "f", "", "not", "vietato"}:
        return False
    raise ValueError(f"invalid boolean: {value!r}")


def _as_list(value: Any) -> list[str]:
    """Parse a pipe-separated (CSV) or JSON (TBX2) list into a Python list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    items = [p.strip() for p in str(value).split(_LIST_SEP)]
    return [p for p in items if p]


def _validate_term_type(value: Any) -> str:
    text = _clean(value).strip().upper()
    if not text:
        raise ValueError("term_type is required")
    if text not in TERM_TYPES:
        raise ValueError(
            f"unknown term_type {text!r}; allowed: {', '.join(sorted(TERM_TYPES))}"
        )
    return text


def _validate_it_gender(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in IT_GENDERS:
        raise ValueError(
            f"invalid IT gender {text!r}; allowed: {', '.join(sorted(IT_GENDERS))}"
        )
    return text


def _validate_it_number(value: Any) -> str | None:
    text = _clean(value).strip().lower()
    if not text:
        return None
    if text not in IT_NUMBERS:
        raise ValueError(
            f"invalid IT number {text!r}; allowed: {', '.join(sorted(IT_NUMBERS))}"
        )
    return text


def _validate_status(value: Any) -> str:
    text = _clean(value).strip().lower()
    if not text:
        return "proposed"
    if text not in TERM_STATUSES:
        raise ValueError(
            f"unknown status {text!r}; allowed: {', '.join(sorted(TERM_STATUSES))}"
        )
    return text


def _clean_term(row: Mapping[str, Any], row_no: int) -> dict:
    """Validate one incoming row and return a normalised term dict.

    Raises ``ValueError`` with a human-readable message on the first problem
    so the caller can build a row-level error report.
    """
    source = _clean(row.get("source_term")).strip()
    if not source:
        raise ValueError(f"row {row_no}: source_term is required")

    target = _clean(row.get("target_term")).strip() or None
    term_type = _validate_term_type(row.get("term_type"))
    preferred = _as_bool(row.get("preferred", True))
    forbidden = _as_list(row.get("forbidden_targets"))
    gender = _validate_it_gender(row.get("grammatical_gender_it"))
    number = _validate_it_number(row.get("grammatical_number"))
    inflection = _clean(row.get("inflection_notes")).strip() or None
    usage = _clean(row.get("usage_notes")).strip() or None
    status = _validate_status(row.get("status"))

    return {
        "source_term": source,
        "target_term": target,
        "term_type": term_type,
        "preferred": preferred,
        "forbidden_targets": forbidden,
        "grammatical_gender_it": gender,
        "grammatical_number": number,
        "inflection_notes": inflection,
        "usage_notes": usage,
        "status": status,
    }


# --- CSV --------------------------------------------------------------------
def import_csv(text: str) -> tuple[list[dict], list[dict]]:
    """Parse CSV text into ``(terms, errors)``.

    ``terms`` is the list of validated, normalised term dicts ready to be
    inserted; ``errors`` is a per-failed-row report of
    ``{"row": N, "error": "..."}``. A row that fails validation is skipped;
    valid rows are still returned so the caller can import the good ones
    even when some rows are bad (§12.3 partial-import contract).
    """
    reader = csv.DictReader(io.StringIO(text))
    terms: list[dict] = []
    errors: list[dict] = []
    for i, raw in enumerate(reader, start=1):
        # DictReader keeps unknown columns under None; drop empties.
        row = {k: v for k, v in raw.items() if k is not None and v is not None}
        try:
            terms.append(_clean_term(row, i))
        except ValueError as exc:
            errors.append({"row": i, "error": str(exc)})
    return terms, errors


def export_csv(terms: Iterable[Mapping[str, Any]]) -> str:
    """Serialise an iterable of term-like mappings to CSV text."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for t in terms:
        writer.writerow([
            _clean(t.get("source_term")),
            _clean(t.get("target_term")),
            _clean(t.get("term_type")),
            _TRUE if t.get("preferred") else _FALSE,
            _LIST_SEP.join(t.get("forbidden_targets") or []),
            _clean(t.get("grammatical_gender_it")),
            _clean(t.get("grammatical_number")),
            _clean(t.get("inflection_notes")),
            _clean(t.get("usage_notes")),
            _clean(t.get("status")),
        ])
    return buf.getvalue()


# --- TBX (TEI Simple Bilingual Glossary) ------------------------------------
def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def export_tbx(terms: Iterable[Mapping[str, Any]]) -> str:
    """Serialise term-like mappings to a TBX2 bilingual glossary (TEI).

    The head gloss is the Italian target; each entry carries the English
    source and, on the ``<entry>`` element, the metadata the PRD §6.5/§7.3
    require (type, preferred, IT gender/number, status, forbidden targets).
    """
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<TEI xmlns="http://www.tei-c.org/ns/1.0">',
        "  <teiHeader>",
        "    <fileDesc>",
        "      <head>Trans termbase export (TBX2)</head>",
        "    </fileDesc>",
        "  </teiHeader>",
        "  <text>",
        "    <group>",
    ]
    for t in terms:
        entry_id = f"e{len(parts)}"
        source = _xml_escape(_clean(t.get("source_term")) or "")
        target = _xml_escape(_clean(t.get("target_term")) or "")
        term_type = _xml_escape(_clean(t.get("term_type")) or "")
        status = _xml_escape(_clean(t.get("status")) or "proposed")
        gender = _clean(t.get("grammatical_gender_it") or "")
        number = _clean(t.get("grammatical_number") or "")
        preferred = _TRUE if t.get("preferred") else _FALSE
        forbidden = t.get("forbidden_targets") or []
        inflection = _xml_escape(_clean(t.get("inflection_notes")) or "")
        usage = _xml_escape(_clean(t.get("usage_notes")) or "")

        parts.append("      <entry")
        parts.append(
            f'          type="{term_type}"'
            f' preferred="{preferred}" status="{status}"'
        )
        if gender:
            parts.append(f'          gender_it="{_xml_escape(gender)}"')
        if number:
            parts.append(f'          number_it="{_xml_escape(number)}"')
        parts.append("        >")
        parts.append("          <head>")
        parts.append(
            f'            <gloss><ref target="#{entry_id}">{target}</ref></gloss>'
        )
        parts.append("          </head>")
        parts.append('          <glossgrp lang="it">')
        parts.append("            <lg>")
        parts.append(
            f'              <l><ref target="#{entry_id}">{target}</ref></l>'
        )
        parts.append("            </lg>")
        parts.append("          </glossgrp>")
        parts.append('          <glossgrp lang="en">')
        parts.append("            <lg>")
        parts.append(f"              <l>{source}</l>")
        parts.append("            </lg>")
        parts.append("          </glossgrp>")
        notes: list[str] = []
        if inflection:
            notes.append(f'            <note type="inflection">{inflection}</note>')
        if usage:
            notes.append(f'            <note type="usage">{usage}</note>')
        for f in forbidden:
            notes.append(
                f'            <note type="forbidden">{_xml_escape(f)}</note>'
            )
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
    """Parse a TBX2 glossary into ``(terms, errors``.

    Uses :mod:`xml.etree.ElementTree`; namespace prefixes are stripped so the
    reader is tolerant of both namespaced and bare elements.
    """
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    def _by_desc(parent: "ET.Element", name: str) -> list:
        """All *direct* children of ``parent`` whose local tag is ``name``.

        Like :meth:`Element.findall` but matched on the local (namespace-free)
        name so it works for both namespaced and bare TEI elements.
        """
        return [c for c in parent if _local(c.tag) == name]

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [], [{"row": 0, "error": f"invalid XML: {exc}"}]

    terms: list[dict] = []
    errors: list[dict] = []
    # Match by *local* tag name so the reader is tolerant of namespaced
    # (xmlns) and bare TEI elements alike -- a bare tag passed to iter()/
    # findall() only matches elements with no namespace, which would skip
    # every <entry> in a real TBX2 document.
    def _by(name: str) -> list:
        return [e for e in root.iter() if _local(e.tag) == name]

    for i, entry in enumerate(_by("entry"), start=1):
        try:
            source = None
            target = None
            for grp in _by_desc(entry, "glossgrp"):
                lang = (grp.get("lang") or "").lower()
                # <l> is nested inside <lg> inside <glossgrp>, so search all
                # descendants of the glossgrp rather than only direct children.
                ls = [c for c in grp.iter() if _local(c.tag) == "l"]
                gloss = " ".join(_l_text(l) for l in ls)
                if lang == "it":
                    target = gloss.strip()
                elif lang in ("en", "eng"):
                    source = gloss.strip()
            # head gloss fallback (some TBX put the head in <head>/<gloss>)
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

            term = {
                "source_term": source,
                "target_term": target,
                "term_type": _attr("type") or "CONCEPT_TERM",
                "preferred": _attr("preferred").lower() == "true",
                "grammatical_gender_it": _attr("gender_it") or None,
                "grammatical_number": _attr("number_it") or None,
                "status": _attr("status") or "proposed",
            }
            # <desc>/<note> carry inflection/usage/forbidden metadata.
            notes: list = []
            for desc in _by_desc(entry, "desc"):
                notes.extend(_by_desc(desc, "note"))
            for note in notes:
                ntype = (note.get("type") or "").lower()
                if ntype == "inflection":
                    term["inflection_notes"] = note.text or ""
                elif ntype == "usage":
                    term["usage_notes"] = note.text or ""
                elif ntype == "forbidden" and note.text:
                    term.setdefault("forbidden_targets", []).append(note.text)
            term["forbidden_targets"] = _as_list(term.get("forbidden_targets"))

            terms.append(_clean_term(term, i))
        except ValueError as exc:
            errors.append({"row": i, "error": str(exc)})
    return terms, errors


def _l_text(l: "ET.Element") -> str:
    """Return the readable text of a TBX ``<l`` gloss line, dropping refs.

    Uses :meth:`itertext` so text inside a ``<ref>`` child (as the export
    produces) is still recovered.
    """
    return "".join(l.itertext())


# --- §7.3 entity selection --------------------------------------------------
_WORD_RE = re.compile(r"[a-zà-ÿ'][a-zà-ÿ]*", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _WORD_RE.findall(text or "")}


def _mentions(text: str, terms: Iterable[str]) -> bool:
    lowered = (text or "").lower()
    return any(t.lower() and t.lower() in lowered for t in terms)


def select_entities_for_block(
    *,
    block_text: str,
    preceding_texts: Iterable[str],
    coref_ids: Iterable[str] = (),
    entities: Iterable[Mapping[str, Any]] = (),
    terms: Iterable[Mapping[str, Any]] = (),
    max_entities: int = 30,
    max_terms: int = 20,
) -> dict:
    """§7.3 selection of entities + terms to attach to one LLM block.

    Rules (PRD §7.3):

    * an **entity** is selected when it is *mentioned in the block*, *in the
      two preceding segments*, or *reached by coreference from the chapter*;
    * a **term** is selected when it is high-priority (``preferred`` or
      ``approved`` and lexically overlaps the block;
    * caps: ``max_entities`` (default 30 and ``max_terms`` (default 20).

    Each returned entry carries the §7.3 payload: source, target form, type,
    IT gender, IT number, alias, policy, notes and priority.
    """
    block_text = block_text or ""
    block_tokens = _tokens(block_text)
    preceding = " ".join(preceding_texts or [])
    coref = {str(c).lower() for c in coref_ids}

    # --- entities ---------------------------------------------------------
    scored: list[tuple[int, str, dict]] = []
    for e in entities:
        eid = str(e.get("id") or "").lower()
        src = e.get("canonical_source") or ""
        aliases = [a for a in (e.get("aliases") or []) if a]
        in_block = _mentions(block_text, [src, *aliases])
        in_preceding = _mentions(preceding, [src, *aliases])
        via_coref = eid in coref
        if not (in_block or in_preceding or via_coref):
            continue
        if in_block:
            prio = 3
        elif in_preceding:
            prio = 2
        else:
            prio = 1
        scored.append((prio, src.lower(), e))

    # priority desc, then natural (human) alphabetical tie-break so that
    # ``block-1`` sorts before ``block-10``.
    def _natural(text: str) -> list:
        return [int(p) if p.isdigit() else p
                for p in re.split(r"(\d+)", text or "")]

    scored.sort(key=lambda x: (-x[0], _natural(x[1] or "")))
    selected_entities = [_entry(e) for _, _, e in scored[:max_entities]]

    # --- terms ------------------------------------------------------------
    selected_terms: list[dict] = []
    for t in terms:
        if not (t.get("preferred") or t.get("status") == "approved"):
            continue
        st = (t.get("source_term") or "").lower()
        if not st:
            continue
        overlap = any(tok in block_tokens for tok in _tokens(st)) or (
            st.strip() in block_text.lower()
        )
        if not overlap:
            continue
        selected_terms.append(_term_entry(t))
        if len(selected_terms) >= max_terms:
            break

    return {
        "entities": selected_entities,
        "terms": selected_terms,
        "counts": {
            "entities_selected": len(selected_entities),
            "terms_selected": len(selected_terms),
            "max_entities": max_entities,
            "max_terms": max_terms,
        },
    }


def _entry(e: Mapping[str, Any]) -> dict:
    """§7.3 payload for an entity."""
    return {
        "id": str(e.get("id")),
        "source": e.get("canonical_source"),
        "target": e.get("canonical_target"),
        "type": e.get("entity_type"),
        "italian_grammatical_gender": e.get("italian_grammatical_gender"),
        "italian_grammatical_number": e.get("grammatical_number"),
        "aliases": list(e.get("aliases") or []),
        "policy": e.get("translation_policy"),
        "notes": e.get("notes"),
        "priority": "high" if e.get("priority") == "block_batch" else "normal",
    }


def _term_entry(t: Mapping[str, Any]) -> dict:
    """§7.3 payload for a term."""
    return {
        "id": str(t.get("id")),
        "source": t.get("source_term"),
        "target": t.get("target_term"),
        "type": t.get("term_type"),
        "italian_grammatical_gender": t.get("grammatical_gender_it"),
        "italian_grammatical_number": t.get("grammatical_number"),
        "aliases": [],
        "policy": "preferred" if t.get("preferred") else "contextual",
        "notes": t.get("usage_notes"),
        "priority": "high" if t.get("preferred") else "normal",
    }
