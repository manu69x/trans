"""Hermetic schema validation for the CAT export formats (PRD §15.4 / AC1, AC2).

The platform is local-only, so validation is done *hermetically* against the
official schemas bundled under :mod:`.export.schemas`:

* :func:`validate_xliff` -- XLIFF 2.1 against the OASIS
  ``xliff_core_2.1.xsd`` (with its imported ``w3c/xml.xsd``);
* :func:`validate_tmx` -- TMX 1.4 against the LISA ``tmx14.dtd``.

Both return the list of human-readable error strings (empty == valid). The
export routes call these *after* writing the file, so an XLIFF that fails the
OASIS XSD (or a TMX that fails the DTD) never ships; the test-suite ACs build
on the same functions so a regression in either writer fails CI.
"""
from __future__ import annotations

import os

from lxml import etree

#: The official schema files bundled with the platform.
SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), "schemas")
XLIFF_XSD_PATH = os.path.join(SCHEMAS_DIR, "xliff_core_2.1.xsd")
TMX_DTD_PATH = os.path.join(SCHEMAS_DIR, "tmx14.dtd")

# Cache the compiled validators (they are expensive to build).
_xliff_schema: etree.XMLSchema | None = None
_tmx_dtd: etree.DTD | None = None


def _get_xliff_schema() -> etree.XMLSchema:
    global _xliff_schema
    if _xliff_schema is None:
        _xliff_schema = etree.XMLSchema(etree.parse(XLIFF_XSD_PATH))
    return _xliff_schema


def _get_tmx_dtd() -> etree.DTD:
    global _tmx_dtd
    if _tmx_dtd is None:
        _tmx_dtd = etree.DTD(TMX_DTD_PATH)
    return _tmx_dtd


def _parse(data: bytes | str) -> etree._Element:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return etree.fromstring(data)


def validate_xliff(data: bytes | str) -> list[str]:
    """Return the OASIS XLIFF 2.1 XSD validation errors (empty == valid).

    A syntactically invalid document yields a single parse-error entry.
    """
    try:
        doc = _parse(data)
    except etree.XMLSyntaxError as exc:
        return [f"XML parse error: {exc}"]
    schema = _get_xliff_schema()
    if schema.validate(doc):
        return []
    return [
        f"{e.line}: {e.message} ({e.type})" for e in schema.error_log
    ]


def validate_tmx(data: bytes | str) -> list[str]:
    """Return the TMX 1.4 DTD validation errors (empty == valid)."""
    try:
        doc = _parse(data)
    except etree.XMLSyntaxError as exc:
        return [f"XML parse error: {exc}"]
    dtd = _get_tmx_dtd()
    if dtd.validate(doc):
        return []
    return [
        f"{e.line}: {e.message} ({e.type})" for e in dtd.error_log
    ]
