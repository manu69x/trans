"""Export manifest (PRD §15.4: "ogni export conserva manifest").

Every export carries a manifest that makes it reproducible and auditable:

* project version + timestamp (immutable, §14)
* segment counts, verifiable against the DB (§15.4 AC3)
* the model(s) used for the project's translations
* the glossary / TM snapshots that were current at export time

The manifest is emitted both as a JSON document (embedded in the EPUB and
returned by the preview endpoint) and as part of the audit log (§13.1).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExportManifest:
    """The §15.4 manifest for one export."""

    format: str  # docx | epub | html
    project_id: str
    title: str
    version: str
    generated_at: str
    source_language: str
    target_language: str
    genre_profile: str
    project_status: str
    models: list[str]
    counts: dict
    include_drafts: bool
    watermarked: bool
    glossary_snapshot_id: str | None = None
    tm_snapshot_id: str | None = None
    audit_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def build_manifest(
    *,
    exporter,
    plan: dict,
    fmt: str,
    version: str | None,
    models: list[str] | None,
    glossary_snapshot_id: str | None = None,
    tm_snapshot_id: str | None = None,
    audit_id: str | None = None,
) -> ExportManifest:
    """Assemble the manifest from the exporter + the effective plan.

    ``version`` is the project version; when omitted it is derived from the
    project's creation timestamp so repeated exports of an unchanged project
    are stable.
    """
    counts = plan["counts"]
    if version is None:
        created = exporter.created_at
        if created is not None:
            try:
                version = created.replace(microsecond=0).isoformat()
            except AttributeError:
                version = _now_iso()
        else:
            version = _now_iso()
    if models is None:
        models = [
            m
            for m in (exporter.translation_model_id, exporter.text_model_id)
            if m
        ]

    return ExportManifest(
        format=fmt,
        project_id=exporter.project_id,
        title=exporter.title,
        version=version,
        generated_at=_now_iso(),
        source_language=exporter.source_language,
        target_language=exporter.target_language,
        genre_profile=exporter.genre_profile,
        project_status=exporter.status,
        models=models,
        counts=counts,
        include_drafts=plan["include_drafts"],
        watermarked=bool(plan.get("watermark", plan.get("force_watermark", False))),
        glossary_snapshot_id=glossary_snapshot_id,
        tm_snapshot_id=tm_snapshot_id,
        audit_id=audit_id,
    )
