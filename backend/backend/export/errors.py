"""Errors for the export subsystem (PRD §15.4 gating)."""
from __future__ import annotations


class ExportError(RuntimeError):
    """Base error for every export failure.

    Carries a stable ``code`` so the API layer can return a structured,
    non-leaking error (PRD §15.4 / §13.1).
    """

    code: str = "export_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class LoadError(ExportError):
    """The project cannot be loaded for export.

    Raised when the project is missing, has no structure/segments, or is in
    a state that makes an export meaningless (e.g. still importing).
    """

    code = "project_unexportable"


class NoApprovedSegments(ExportError):
    """There are approved segments, but the caller asked for approved-only.

    This is the §15.4 "block or force watermark" gate: an export that would
    otherwise ship drafts must be refused unless the caller explicitly opts
    into drafts (with the watermark forced on).
    """

    code = "no_approved_segments"


class WatermarkRequired(ExportError):
    """A drafts export requires the explicit watermark.

    Raised when the caller asked for ``include_drafts`` but did not also
    request the watermark (§13.1: watermark opzionale su bozza, ma qui
    forzato perché la bozza è l'unico contenuto disponibile).
    """

    code = "watermark_required"
