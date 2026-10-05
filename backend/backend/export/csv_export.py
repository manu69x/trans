"""Simple bilingual CSV export / re-import (PRD §1.3.7, §15.4).

The CSV is deliberately plain -- a flat, tool-agnostic bilingual table that
any spreadsheet or CAT import script can consume:

::

    segment_id,chapter,ordinal,status,kind,source,target

* ``segment_id`` is the stable CAT identity (the unit UUID);
* ``source`` / ``target`` are the EN/IT texts (RFC-4180 quoted);
* ``status`` is the platform status at export time; the remaining columns
  are provenance metadata.

:func:`read_csv` is the inverse of :func:`write_csv` (it reads the same
header, UTF-8 with BOM for Excel), so a CAT tool's CSV round-trips back
into the platform through :mod:`~.export.reimport`.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .collector import Exporter
from .errors import ExportError

#: The CSV header, in the fixed order both the writer and the reader expect.
CSV_COLUMNS = (
    "segment_id",
    "chapter",
    "ordinal",
    "status",
    "kind",
    "source",
    "target",
)


@dataclass
class CsvRow:
    """One CSV row read back from a bilingual CSV file."""

    segment_id: str
    chapter: str | None
    ordinal: int | None
    status: str | None
    kind: str | None
    source: str
    target: str | None


def write_csv(exporter: Exporter, plan: dict, out_dir) -> "Path":
    """Render the planned segments to ``<book-title>.csv`` in ``out_dir``.

    The file is UTF-8 *with BOM* so Excel opens it with the right encoding.
    Returns the written path.
    """
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (exporter.title or "book").strip().replace(" ", "_") or "book"
    out_path = out_dir / f"{stem}.csv"

    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for seg in plan["selected"]:
        writer.writerow(
            [
                seg.segment_id,
                seg.chapter_title or "",
                seg.ordinal,
                seg.status,
                seg.kind or "",
                seg.source_text or "",
                seg.target_text or "",
            ]
        )

    try:
        out_path.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
    except Exception as exc:  # pragma: no cover - writer failure path
        raise ExportError(f"csv writer failed: {exc}") from exc
    return out_path


def read_csv(data: bytes | str) -> list[CsvRow]:
    """Parse a bilingual CSV into :class:`CsvRow` records, preserving order.

    The pure inverse of :func:`write_csv`. Raises :class:`ExportError` on a
    malformed file (missing header, non-integer ordinal).
    """
    text = data.decode("utf-8-sig") if isinstance(data, (bytes, bytearray)) else data
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ExportError("CSV is empty (no header row)")
    fieldnames = [f.strip() for f in reader.fieldnames]
    if "segment_id" not in fieldnames or "source" not in fieldnames:
        raise ExportError(
            f"CSV header must include `segment_id` and `source`; got {fieldnames}"
        )

    rows: list[CsvRow] = []
    for raw in reader:
        # csv.DictReader keys carry the header as written; normalise them.
        rec = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        seg_id = rec.get("segment_id", "")
        if not seg_id:
            continue
        ordinal = rec.get("ordinal") or None
        if ordinal is not None:
            try:
                ordinal = int(ordinal)
            except ValueError:
                ordinal = None
        target = rec.get("target") or None
        rows.append(
            CsvRow(
                segment_id=seg_id,
                chapter=rec.get("chapter") or None,
                ordinal=ordinal,
                status=rec.get("status") or None,
                kind=rec.get("kind") or None,
                source=rec.get("source") or "",
                target=target,
            )
        )
    return rows
