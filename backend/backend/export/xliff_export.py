"""XLIFF 2.1 bilingual export for CAT hand-off (PRD §1.3.7, §15.4).

Builds an :file:`.xlf` (XLIFF 2.1, namespace
``urn:oasis:names:tc:xliff:document:2.0``) with one ``<unit>`` per
:class:`~.export.collector.ExportSegment`:

* ``id``    -- the segment's UUID (the stable CAT identity, NMTOKEN-safe);
* ``name``  -- the human-readable ``<chapter>-<ordinal>`` label;
* ``type``  -- the §5.4.2 paragraph kind, a namespaced user-defined value
  (the XLIFF 2.1 XSD types ``unit@type`` as ``userDefinedValue`` with the
  pattern ``[^\\s:]+:[^\\s:]+``);
* ``<segment>`` -- the EN source and the IT target;
* ``<segment @state>`` -- the XLIFF 2.1 state derived from the platform
  status (``approved``→``final``, ``machine_draft``→``translated``,
  else ``initial``);
* ``<notes>``   -- the platform metadata as standard XLIFF machine-readable
  ``<note category="trans:...">value</note>`` entries (``trans:status``,
  ``trans:chapter``, ``trans:ordinal``). ``@category`` is the conventional
  hook external CAT tools (Okapi, MemoQ, ...) use for non-localised metadata,
  so the values round-trip without re-scraping the document.

:func:`read_xliff` parses a file back into a list of :class:`XliffUnit`
records -- the pure inverse of :func:`write_xliff` -- so a CAT tool's edits
can be re-imported without re-scraping the document.
"""
from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from .collector import Exporter, ExportSegment
from .errors import ExportError

# The XLIFF 2.0/2.1 document namespace (the version *attribute* is separate
# and carries the literal ``2.1``).
XLIFF_NS = "urn:oasis:names:tc:xliff:document:2.0"
XLIFF_VERSION = "2.1"

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# The ``@category`` keys Trans uses for machine-readable unit metadata.
CAT_STATUS = "trans:status"
CAT_CHAPTER = "trans:chapter"
CAT_ORDINAL = "trans:ordinal"
CAT_KIND = "trans:kind"


def _ns(tag: str) -> str:
    """Clark-notation name for a tag in the XLIFF namespace."""
    return f"{{{XLIFF_NS}}}{tag}"


# The platform's segment status -> the XLIFF 2.1 ``state`` value the XSD
# accepts (``initial`` | ``translated`` | ``reviewed`` | ``final``).
_STATUS_TO_STATE = {
    "untranslated": "initial",
    "machine_draft": "translated",
    "reviewed": "reviewed",
    "approved": "final",
}

# Inverse used on re-import: XLIFF state -> the closest platform status.
_STATE_TO_STATUS = {
    "initial": "untranslated",
    "translated": "machine_draft",
    "reviewed": "machine_draft",
    "final": "approved",
}


def status_to_state(status: str) -> str:
    """The XLIFF 2.1 ``state`` for a platform segment status."""
    return _STATUS_TO_STATE.get(status, "initial")


def state_to_status(state: str | None) -> str:
    """The platform status for an XLIFF 2.1 ``state`` (fallback ``untranslated``)."""
    if state is None:
        return "untranslated"
    return _STATE_TO_STATUS.get(state, "untranslated")


@dataclass
class XliffUnit:
    """One ``<unit>`` read back from an :file:`.xlf` file."""

    id: str
    name: str | None
    kind: str | None
    state: str | None
    source: str
    target: str | None
    status: str | None = None
    chapter: str | None = None
    ordinal: int | None = None

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"


def _add_text(parent: etree._Element, tag: str, text: str, lang: str) -> None:
    """A ``<source>``/``<target>`` child with preserved spaces and a lang."""
    el = etree.SubElement(parent, _ns(tag))
    el.set(_XML_LANG, lang)
    el.set(_XML_SPACE, "preserve")
    if text:
        el.text = text


def _add_cat_note(notes: etree._Element, category: str, value: str) -> None:
    """A standard machine-readable ``<note category=...>value</note>``."""
    if value is None:
        return
    n = etree.SubElement(notes, _ns("note"))
    n.set("category", category)
    n.text = str(value)


def _write_unit(
    file_el: etree._Element,
    seg: ExportSegment,
    src_lang: str,
    trg_lang: str,
) -> None:
    """Emit one ``<unit>`` for a planned segment."""
    unit = etree.SubElement(file_el, _ns("unit"))
    unit.set("id", seg.segment_id)
    label = (
        f"{seg.chapter_title}-{seg.ordinal}"
        if seg.chapter_title
        else str(seg.ordinal)
    )
    unit.set("name", label)
    # ``userDefinedValue`` pattern requires the ``ns:token`` form.
    unit.set("type", f"trans:{seg.kind or 'narration'}")

    notes = etree.SubElement(unit, _ns("notes"))
    _add_cat_note(notes, CAT_STATUS, seg.status)
    if seg.chapter_title:
        _add_cat_note(notes, CAT_CHAPTER, seg.chapter_title)
    _add_cat_note(notes, CAT_ORDINAL, seg.ordinal)
    if seg.kind:
        _add_cat_note(notes, CAT_KIND, seg.kind)

    segment = etree.SubElement(unit, _ns("segment"))
    segment.set("state", status_to_state(seg.status))
    _add_text(segment, "source", seg.source_text or "", src_lang)
    _add_text(segment, "target", seg.target_text or "", trg_lang)


def write_xliff(exporter: Exporter, plan: dict, out_dir) -> "Path":
    """Render the planned segments to ``<book-title>.xlf`` in ``out_dir``.

    The file-level ``<notes>`` carry the §15.4 manifest summary so the file
    is self-describing. Returns the written path. Raises
    :class:`~.errors.ExportError` on a writer failure.
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.xlf"

    src_lang = (exporter.source_language or "en")[:2]
    trg_lang = (exporter.target_language or "it")[:2]

    xliff = etree.Element(
        _ns("xliff"),
        nsmap={None: XLIFF_NS},
    )
    xliff.set("version", XLIFF_VERSION)
    xliff.set("srcLang", src_lang)
    xliff.set("trgLang", trg_lang)

    file_el = etree.SubElement(xliff, _ns("file"))
    file_el.set("id", exporter.project_id)
    file_el.set("original", stem + ".src")
    file_el.set("canResegment", "no")

    # §15.4 manifest note (self-describing file header).
    manifest = plan.get("manifest") or {}
    if manifest:
        notes = etree.SubElement(file_el, _ns("notes"))
        n = etree.SubElement(notes, _ns("note"))
        n.set("category", "trans:manifest")
        parts = [
            f"version={manifest.get('version')}",
            f"generated_at={manifest.get('generated_at')}",
            f"source={manifest.get('source_language')}",
            f"target={manifest.get('target_language')}",
            f"genre={manifest.get('genre_profile')}",
            f"watermarked={manifest.get('watermarked')}",
            f"counts={manifest.get('counts')}",
        ]
        n.text = " ".join(p for p in parts if p)

    for seg in plan["selected"]:
        _write_unit(file_el, seg, src_lang, trg_lang)

    try:
        etree.ElementTree(xliff).write(
            out_path,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True,
        )
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"xliff writer failed: {exc}") from exc
    return out_path


def _read_unit(el: etree._Element) -> XliffUnit:
    """Parse one ``<unit>`` element into an :class:`XliffUnit`."""
    # ``state`` lives on the ``<segment>`` child (XLIFF 2.1 placement), not on
    # the ``<unit>`` itself -- reading it from the unit would silently drop
    # the CAT tool's state on re-import (AC1 "senza perdita di stato").
    segment_el = el.find(_ns("segment"))
    state = segment_el.get("state") if segment_el is not None else None
    kind = el.get("type")
    if kind and kind.startswith("trans:"):
        kind = kind[len("trans:"):]

    status: str | None = None
    chapter: str | None = None
    ordinal: int | None = None
    for note in el.findall(f"{_ns('notes')}/{_ns('note')}"):
        cat = note.get("category")
        text = (note.text or "").strip()
        if cat == CAT_STATUS:
            status = text or None
        elif cat == CAT_CHAPTER:
            chapter = text or None
        elif cat == CAT_ORDINAL:
            try:
                ordinal = int(text)
            except (TypeError, ValueError):
                ordinal = None

    src_el = el.find(f"{_ns('segment')}/{_ns('source')}")
    tgt_el = el.find(f"{_ns('segment')}/{_ns('target')}")
    source = (src_el.text or "") if src_el is not None else ""
    target = (
        tgt_el.text
        if tgt_el is not None and tgt_el.text is not None
        else None
    )

    # Fall back to the XLIFF state when no explicit trans:status is present.
    if status is None:
        status = state_to_status(state)

    return XliffUnit(
        id=el.get("id") or "",
        name=el.get("name"),
        kind=kind,
        state=state,
        source=source,
        target=target,
        status=status,
        chapter=chapter,
        ordinal=ordinal,
    )


def read_xliff(data: bytes | str) -> list[XliffUnit]:
    """Parse an :file:`.xlf` (XLIFF 2.1) document into a list of
    :class:`XliffUnit`, preserving order. The pure inverse of
    :func:`write_xliff`. Raises :class:`~.errors.ExportError` on a
    malformed document.
    """
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise ExportError(f"XLIFF parse error: {exc}") from exc

    units: list[XliffUnit] = []
    for unit_el in root.iter(_ns("unit")):
        units.append(_read_unit(unit_el))
    return units
