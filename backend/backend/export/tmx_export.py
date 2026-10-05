"""TMX 1.4 export of the approved translation memory (PRD §1.3.7, §15.4, §7.2).

Renders the project's :class:`~.models.translation.TranslationMemoryEntry`
rows to a :file:`.tmx` conforming to the LISA/OASIS TMX 1.4 DTD
(:file:`schemas/tmx14.dtd`):

::

    <tmx version="1.4">
      <header creationtool="trans" creationtoolversion="..." segtype="sentence"
              o-tmf="xlf20" adminlang="it" srclang="en" datatype="plaintext"/>
      <body>
        <tu tuid="...">
          <tuv xml:lang="en"><seg>...</seg></tuv>
          <tuv xml:lang="it"><seg>...</seg></tuv>
        </tu>
        ...
      </body>
    </tmx>

* one ``<tu>`` per TM entry, ``tuid`` = the entry UUID;
* the two ``<tuv>`` carry the source and the approved target;
* the DTD-required header attributes are filled from the project's language
  pair and platform identity (see :func:`validate_tmx`, which checks the
  output against the official DTD so an external CAT tool -- Okapi,
  MemoQ, Wordfast -- can import it).

:func:`read_tmx` parses a ``.tmx`` back into :class:`TmxUnit` records so the
exported memory can be diffed (or re-imported by a consumer that edits the
TMX) without re-scraping the file.
"""
from __future__ import annotations

from dataclasses import dataclass

from lxml import etree

from .collector import Exporter
from .errors import ExportError

TMX_VERSION = "1.4"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@dataclass
class TmxUnit:
    """One ``<tu>`` read back from a TMX 1.4 file."""

    tuid: str | None
    source: str | None
    target: str | None
    source_lang: str | None = None
    target_lang: str | None = None


def write_tmx(exporter: Exporter, tm_entries: list[dict], out_dir) -> "Path":
    """Render ``tm_entries`` to ``<book-title>.tmx`` in ``out_dir``.

    ``tm_entries`` is a list of plain dicts with the shape of
    :class:`~.models.translation.TranslationMemoryEntry` rows:
    ``{"id", "source_original", "target_approved", ...}``.

    Returns the written path. Raises :class:`ExportError` on a writer
    failure.
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.tmx"

    tmx = etree.Element("tmx")
    tmx.set("version", TMX_VERSION)

    src_lang = (exporter.source_language or "en")[:2]
    trg_lang = (exporter.target_language or "it")[:2]
    admin_lang = (exporter.source_language or "it")[:2]

    header = etree.SubElement(tmx, "header")
    header.set("creationtool", "trans")
    header.set("creationtoolversion", "0.1.0")
    header.set("segtype", "sentence")
    header.set("o-tmf", "xlf20")
    header.set("adminlang", admin_lang)
    header.set("srclang", src_lang)
    header.set("datatype", "plaintext")
    # The manifest summary doubles as the TMX ``<note>`` (self-describing).
    manifest = None
    if exporter.created_at is not None:
        try:
            manifest = exporter.created_at.replace(microsecond=0).isoformat()
        except AttributeError:  # pragma: no cover
            manifest = None
    note = etree.SubElement(header, "note")
    note.text = (
        f"project={exporter.project_id} title={exporter.title} "
        f"version={manifest} entries={len(tm_entries)}"
    )

    body = etree.SubElement(tmx, "body")
    for entry in tm_entries:
        tu = etree.SubElement(body, "tu")
        if entry.get("id"):
            tu.set("tuid", str(entry["id"]))
        src_tuv = etree.SubElement(tu, "tuv")
        src_tuv.set(_XML_LANG, src_lang)
        src_seg = etree.SubElement(src_tuv, "seg")
        src_seg.text = entry.get("source_original") or ""
        trg_tuv = etree.SubElement(tu, "tuv")
        trg_tuv.set(_XML_LANG, trg_lang)
        trg_seg = etree.SubElement(trg_tuv, "seg")
        trg_seg.text = entry.get("target_approved") or ""

    try:
        etree.ElementTree(tmx).write(
            out_path,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True,
        )
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"tmx writer failed: {exc}") from exc
    return out_path


def read_tmx(data: bytes | str) -> list[TmxUnit]:
    """Parse a TMX 1.4 document into :class:`TmxUnit` records, preserving
    order. The inverse of :func:`write_tmx`. Raises
    :class:`ExportError` on a malformed document.
    """
    try:
        if isinstance(data, str):
            data = data.encode("utf-8")
        root = etree.fromstring(data)
    except etree.XMLSyntaxError as exc:
        raise ExportError(f"TMX parse error: {exc}") from exc

    units: list[TmxUnit] = []
    for tu in root.iter("tu"):
        source: str | None = None
        target: str | None = None
        source_lang: str | None = None
        target_lang: str | None = None
        tuv_count = 0
        for tuv in tu.findall("tuv"):
            lang = tuv.get(_XML_LANG)
            seg = tuv.find("seg")
            text = seg.text or "" if seg is not None else ""
            tuv_count += 1
            if tuv_count == 1:
                source_lang = lang
                source = text
            else:
                target_lang = lang
                target = text
        units.append(
            TmxUnit(
                tuid=tu.get("tuid"),
                source=source,
                target=target,
                source_lang=source_lang,
                target_lang=target_lang,
            )
        )
    return units
