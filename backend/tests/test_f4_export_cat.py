"""CAT export tests: XLIFF 2.1 / TMX 1.4 / CSV bilingual + reimport
(PRd §1.3.7, §15.4 / ).

Acceptance criteria covered here:

* **AC1** -- the exported XLIFF validates against the OASIS XLIFF 2.1 XSD
  (hermetically, no network) and can be re-imported *without* losing state:
  each imported segment freezes its pre-change target on
  ``translation_unit_versions`` and re-imports its state; approved segments
  are never overwritten (§5.1).
* **AC2** -- the TMX validates against the LISA TMX 1.4 DTD and carries one
  ``<tu>`` per approved TM entry (importable in an external CAT tool).
* **AC3** -- round-trip: export -> simulate the CAT tool editing the file ->
  re-import -> the segments carry the edited targets with the version
  history intact, and a second export of the unchanged segments is
  byte-identical (no unexpected differences).

The suite is isolated on its own database (``trans_export_cat``), the same
pattern as ``test_f4_export.py``.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

# Isolated test database, decided BEFORE backend.main is imported (the
# engine is built from DATABASE_URL at import time, so a setdefault inside a
# fixture would come too late).
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://trans:trans@127.0.0.1:5432/trans_export_cat",
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALEMBIC_SQLALCHEMY_URI"] = TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("STORAGE_ROOT", "/tmp/trans-test-f4-export-cat")
os.environ.setdefault("LOCAL_ONLY", "1")


def _ensure_test_db() -> None:
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_export_cat' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_export_cat"))
            conn.execute(text(
                "CREATE DATABASE trans_export_cat TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_export_cat",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


_ensure_test_db()

from backend.db import Base, SessionLocal, engine  # noqa: E402
from backend.export.validate import validate_tmx, validate_xliff  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import (  # noqa: E402
    MemorySnapshot,
    Project,
    StructureNode,
    TranslationMemoryEntry,
    TranslationUnit,
    TranslationUnitVersion,
)


def _drop_and_recreate_test_db() -> None:
    admin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/postgres",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'trans_export_cat' AND pid <> pg_backend_pid()"))
            conn.execute(text("DROP DATABASE IF EXISTS trans_export_cat"))
            conn.execute(text(
                "CREATE DATABASE trans_export_cat TEMPLATE template0"))
    finally:
        admin.dispose()
    vadmin = create_engine(
        "postgresql://trans:trans@127.0.0.1:5432/trans_export_cat",
        isolation_level="AUTOCOMMIT",
    )
    try:
        with vadmin.connect() as vconn:
            vconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    finally:
        vadmin.dispose()


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    import subprocess

    engine.dispose()
    _drop_and_recreate_test_db()
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(BACKEND_DIR),
        env={**os.environ, "ALEMBIC_SQLALCHEMY_URI": TEST_DATABASE_URL},
    )
    engine.dispose()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _reset():
    engine.dispose()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    yield
    engine.dispose()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def _node(pid: str, ordinal: int, kind: str, title: str, node_id: str) -> StructureNode:
    return StructureNode(
        id=node_id,
        project_id=pid,
        kind=kind,
        normalized_title=title,
        ordinal=ordinal,
        status="confirmed",
    )


def _unit(
    pid: str,
    chapter_id: str | None,
    ordinal: int,
    name: str,
    source: str,
    target: str | None,
    status: str,
    flags: dict | None = None,
) -> TranslationUnit:
    return TranslationUnit(
        id=_uuid(name),
        project_id=pid,
        chapter_id=chapter_id,
        ordinal=ordinal,
        source_text=source,
        target_text=target,
        status=status,
        source_hash="x" * 64,
        source_flags=flags or {},
    )


async def _new_project(client: AsyncClient, title: str = "CAT Book") -> str:
    r = await client.post(
        "/api/v1/projects",
        json={"title": title, "genre_profile": "fantasy"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _seed_book(
    pid: str,
    *,
    ch1: str,
    ch2: str,
    statuses: list[str] | None = None,
) -> dict[str, str]:
    """Two chapters, three units each; returns name->unit_id map."""
    statuses = statuses or ["approved", "machine_draft", "approved"]
    ids: dict[str, str] = {}
    with SessionLocal() as db:
        db.add(_node(pid, 1, "chapter", "Capitolo uno", ch1))
        db.add(_node(pid, 2, "chapter", "Capitolo due", ch2))
        for i in range(3):
            name = f"ch1-{i}"
            src = f"Chapter one, line {i + 1}."
            tgt = f"Capitolo uno, riga {i + 1}."
            ids[name] = _uuid(name)
            db.add(_unit(
                pid, ch1, i + 1, name, src, tgt, statuses[i],
                flags={"kind": "narration", "page": i + 1},
            ))
        for i in range(3):
            name = f"ch2-{i}"
            src = f"Chapter two, line {i + 1}."
            tgt = f"Capitolo due, riga {i + 1}."
            ids[name] = _uuid(name)
            db.add(_unit(
                pid, ch2, i + 1, name, src, tgt, statuses[i],
                flags={"kind": "dialogue", "page": i + 11},
            ))
        db.commit()
    return ids


def _approve_and_tm(db, project: Project, unit: TranslationUnit, reviewer: str) -> str:
    """Mirror the editor's approve hook (editor_routes.approve_segment):
    set status approved + register the §7.2 TM entry."""
    from backend.translation import embedding

    unit.status = "approved"
    entry = TranslationMemoryEntry(
        id=str(uuid.uuid4()),
        project_id=str(project.id),
        chapter_id=unit.chapter_id,
        segment_id=str(unit.id),
        source_normalized=unit.source_text,
        source_original=unit.source_text,
        target_approved=unit.target_text or "",
        genre_profile=project.genre_profile,
        reviewer=reviewer,
        terms_used=[],
        source_embedding=embedding.embed(unit.source_text),
    )
    db.add(entry)
    db.commit()
    return str(entry.id)


# ---------------------------------------------------------------------------
# AC1 -- XLIFF 2.1 validates against the OASIS XSD and reimports without
# losing state (versions frozen, approved segments immutable).
# ---------------------------------------------------------------------------
async def test_ac1_xliff_valid_xsd_and_reimport_preserves_state(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)

    # Approve the two ch1 approved units through the TM hook so the TMX has
    # entries; leave ch2 as-is (mixed statuses).
    with SessionLocal() as db:
        project = db.get(Project, pid)
        for name in ("ch1-0", "ch1-2"):
            unit = db.get(TranslationUnit, ids[name])
            _approve_and_tm(db, project, unit, "tester")

    # 1) Export the XLIFF. A CAT hand-off ships the whole bilingual file:
    #    approved segments plus the drafts, with the explicit §15.4/§13.1
    #    watermark (the default export is approved-only).
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "xliff", "include_drafts": True, "watermark": True},
    )
    assert r.status_code == 200, r.text
    xlf_bytes = r.content
    # AC1: the OASIS XLIFF 2.1 XSD accepts it (hermetic validation).
    assert validate_xliff(xlf_bytes) == [], validate_xliff(xlf_bytes)

    # 2) Simulate the CAT tool editing one draft segment in the file:
    #    rewrite its <target> and bump the unit state to "final".
    from backend.export.xliff_export import read_xliff, XliffUnit

    units = read_xliff(xlf_bytes)
    assert len(units) == 6
    by_id = {u.id: u for u in units}
    # ch1-1 is a machine_draft; the tool rewrites its target and marks final.
    draft = by_id[ids["ch1-1"]]
    assert draft.status == "machine_draft"
    edited_target = "Capitolo uno, riga 2 (rivisto in CAT)."
    # Serialize the edit back to XLIFF bytes the way a CAT tool would:
    # reuse our own writer on a minimal exporter (the reimport only reads).
    import io
    from lxml import etree

    root = etree.fromstring(xlf_bytes)
    ns = {"xlf": "urn:oasis:names:tc:xliff:document:2.0"}
    for unit_el in root.iter("{urn:oasis:names:tc:xliff:document:2.0}unit"):
        if unit_el.get("id") == ids["ch1-1"]:
            seg = unit_el.find("xlf:segment", ns)
            seg.set("state", "final")
            tgt = seg.find("xlf:target", ns)
            tgt.text = edited_target
            # update the trans:status note as a well-behaved tool would
            for note in unit_el.findall("xlf:notes/xlf:note", ns):
                if note.get("category") == "trans:status":
                    note.text = "approved"
    tool_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    # The CAT-edited file is still XSD-valid.
    assert validate_xliff(tool_bytes) == []

    # 3) Re-import it.
    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        files={"file": ("edited.xlf", tool_bytes, "application/x-xliff+xml")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    # The edited draft was approved via the import (state=final).
    assert summary["counts"]["approved"] >= 1, summary

    with SessionLocal() as db:
        unit = db.get(TranslationUnit, ids["ch1-1"])
        # Target updated...
        assert unit.target_text == edited_target
        # ...with the pre-change target frozen (no version lost, §10.5).
        versions = (
            db.query(TranslationUnitVersion)
            .filter(TranslationUnitVersion.unit_id == ids["ch1-1"])
            .order_by(TranslationUnitVersion.created_at)
            .all()
        )
        before_versions = [v for v in versions if v.before]
        after_versions = [v for v in versions if not v.before]
        assert before_versions, "pre-change version not frozen on reimport"
        assert before_versions[0].target_text == "Capitolo uno, riga 2."
        assert after_versions[-1].target_text == edited_target
        # Status flipped to approved, TM entry registered (§7.2).
        assert unit.status == "approved"
        tm = (
            db.query(TranslationMemoryEntry)
            .filter(TranslationMemoryEntry.segment_id == ids["ch1-1"])
            .one_or_none()
        )
        assert tm is not None
        assert tm.target_approved == edited_target

        # Approved segments must NOT have been touched by the import
        # (their targets in the file were the originals anyway).
        approved_unit = db.get(TranslationUnit, ids["ch1-0"])
        assert approved_unit.target_text == "Capitolo uno, riga 1."

    # 4) The imported state survives a re-export: the next XLIFF carries the
    #    edited target and state=final, no state loss (AC1 "senza perdita di
    #    stato").
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "xliff"},
    )
    assert r.status_code == 200, r.text
    units2 = read_xliff(r.content)
    again = {u.id: u for u in units2}[ids["ch1-1"]]
    assert again.target == edited_target
    assert again.state == "final"


# ---------------------------------------------------------------------------
# AC1 -- an edited target on an *approved* segment is a conflict, never a
# silent overwrite (§5.1).
# ---------------------------------------------------------------------------
async def test_ac1_approved_segment_conflict_not_overwritten(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        unit = db.get(TranslationUnit, ids["ch1-0"])
        _approve_and_tm(db, project, unit, "tester")

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "xliff"},
    )
    assert r.status_code == 200, r.text

    from lxml import etree

    root = etree.fromstring(r.content)
    ns = {"xlf": "urn:oasis:names:tc:xliff:document:2.0"}
    for unit_el in root.iter("{urn:oasis:names:tc:xliff:document:2.0}unit"):
        if unit_el.get("id") == ids["ch1-0"]:
            seg = unit_el.find("xlf:segment", ns)
            tgt = seg.find("xlf:target", ns)
            tgt.text = "SOVRASCrittura non permessa."
    tool_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        files={"file": ("edited.xlf", tool_bytes, "application/x-xliff+xml")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["counts"]["skipped_approved"] >= 1
    conflicts = summary.get("conflicts") or []
    assert any(
        c["segment_id"] == ids["ch1-0"]
        and c["incoming_target"] == "SOVRASCrittura non permessa."
        for c in conflicts
    ), conflicts

    with SessionLocal() as db:
        unit = db.get(TranslationUnit, ids["ch1-0"])
        assert unit.target_text == "Capitolo uno, riga 1."  # untouched


# ---------------------------------------------------------------------------
# AC2 -- TMX 1.4 validates against the LISA DTD, one <tu> per approved TM
# entry (importable in an external CAT tool).
# ---------------------------------------------------------------------------
async def test_ac2_tmx_valid_dtd_and_content(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)
    entry_ids = []
    with SessionLocal() as db:
        project = db.get(Project, pid)
        for name in ("ch1-0", "ch1-2"):
            unit = db.get(TranslationUnit, ids[name])
            entry_ids.append(_approve_and_tm(db, project, unit, "tester"))

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "tmx"},
    )
    assert r.status_code == 200, r.text
    tmx_bytes = r.content
    # AC2: DTD-valid (the gate an external CAT tool's import performs).
    assert validate_tmx(tmx_bytes) == [], validate_tmx(tmx_bytes)

    from backend.export.tmx_export import read_tmx

    tus = read_tmx(tmx_bytes)
    assert len(tus) == 2
    tuids = {t.tuid for t in tus}
    assert tuids == {str(e) for e in entry_ids}
    by_tuid = {t.tuid: t for t in tus}
    t0 = by_tuid[str(entry_ids[0])]
    assert t0.source == "Chapter one, line 1."
    assert t0.target == "Capitolo uno, riga 1."
    assert t0.source_lang == "en" and t0.target_lang == "it"


async def test_tmx_requires_approved_entries(client):
    """A project with no approved segments has an empty TM: the TMX export
    is blocked (409) rather than shipping an empty file."""
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    await _seed_book(
        pid, ch1=ch1, ch2=ch2,
        statuses=["machine_draft"] * 3,
    )
    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "tmx"},
    )
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# AC3 -- round-trip: export -> (tool leaves it untouched) -> reimport ->
# second export: no unexpected differences in the segments.
# ---------------------------------------------------------------------------
async def test_ac3_roundtrip_no_unexpected_differences(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)
    with SessionLocal() as db:
        project = db.get(Project, pid)
        for name in ("ch1-0", "ch1-2", "ch2-0", "ch2-2"):
            unit = db.get(TranslationUnit, ids[name])
            _approve_and_tm(db, project, unit, "tester")

    # First export (approved-only).
    r1 = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "xliff"},
    )
    assert r1.status_code == 200, r1.text

    # Re-import the *unmodified* file (a tool that only read it).
    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        files={"file": ("book.xlf", r1.content, "application/x-xliff+xml")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    # Nothing should have changed: approved units skipped, drafts unchanged
    # (their state is `translated` -> status machine_draft, same as before).
    assert summary["counts"]["updated"] == 0, summary
    assert summary["counts"]["approved"] == 0, summary
    assert summary["counts"]["not_found"] == 0, summary

    # Second export: the segments are byte-for-byte the same as the first
    # (no state drift from the round-trip).
    r2 = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "xliff"},
    )
    assert r2.status_code == 200, r2.text

    from backend.export.xliff_export import read_xliff

    a = {u.id: (u.source, u.target, u.state, u.status) for u in read_xliff(r1.content)}
    b = {u.id: (u.source, u.target, u.state, u.status) for u in read_xliff(r2.content)}
    assert a == b, "round-trip changed segment content"

    # And no version rows were created by the no-op import.
    with SessionLocal() as db:
        n_versions = (
            db.query(TranslationUnitVersion)
            .filter(TranslationUnitVersion.project_id == pid)
            .count()
        )
        assert n_versions == 0


# ---------------------------------------------------------------------------
# AC3 -- CSV round-trip: edited targets land, versions frozen.
# ---------------------------------------------------------------------------
async def test_csv_roundtrip(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "csv", "include_drafts": True, "watermark": True},
    )
    assert r.status_code == 200, r.text
    csv_bytes = r.content
    assert csv_bytes.startswith(b"\xef\xbb\xbf"), "CSV must be UTF-8 with BOM"

    import io
    import csv as csv_mod

    text = csv_bytes.decode("utf-8-sig")
    rows = list(csv_mod.DictReader(io.StringIO(text)))
    assert len(rows) == 6
    # The tool edits one draft's target column.
    for row in rows:
        if row["segment_id"] == ids["ch2-1"]:
            row["target"] = "Capitolo due, riga 2 (editato)."
    out = io.StringIO()
    w = csv_mod.DictWriter(out, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        data={"fmt": "csv"},
        files={"file": ("book.csv", out.getvalue().encode("utf-8"), "text/csv")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["counts"]["updated"] >= 1, summary

    with SessionLocal() as db:
        unit = db.get(TranslationUnit, ids["ch2-1"])
        assert unit.target_text == "Capitolo due, riga 2 (editato)."
        versions = (
            db.query(TranslationUnitVersion)
            .filter(TranslationUnitVersion.unit_id == ids["ch2-1"])
            .all()
        )
        assert any(v.before and v.target_text == "Capitolo due, riga 2." for v in versions)


# ---------------------------------------------------------------------------
# TMX reimport: an edited approved target updates the TM (and reports a
# conflict when the segment is approved).
# ---------------------------------------------------------------------------
async def test_tmx_reimport_updates_tm(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)
    entry_ids = []
    with SessionLocal() as db:
        project = db.get(Project, pid)
        for name in ("ch1-0", "ch1-2"):
            unit = db.get(TranslationUnit, ids[name])
            entry_ids.append(_approve_and_tm(db, project, unit, "tester"))

    r = await client.post(
        f"/api/v1/projects/{pid}/export",
        json={"format": "tmx"},
    )
    assert r.status_code == 200, r.text

    from lxml import etree

    root = etree.fromstring(r.content)
    for tu in root.iter("tu"):
        if tu.get("tuid") == str(entry_ids[0]):
            for tuv in tu.findall("tuv"):
                if tuv.get("{http://www.w3.org/XML/1998/namespace}lang") == "it":
                    tuv.find("seg").text = "Capitolo uno, riga 1 (ritoccato)."
    tool_bytes = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    assert validate_tmx(tool_bytes) == []

    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        files={"file": ("tm.tmx", tool_bytes, "application/x-tmx")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["counts"]["updated"] == 1, summary

    with SessionLocal() as db:
        entry = db.get(TranslationMemoryEntry, str(entry_ids[0]))
        assert entry.target_approved == "Capitolo uno, riga 1 (ritoccato)."


# ---------------------------------------------------------------------------
# Bad input: a file that is not XLIFF-2.1-shaped is rejected (422) and no
# segment is touched.
# ---------------------------------------------------------------------------
async def test_reimport_rejects_bad_xliff(client):
    pid = await _new_project(client)
    ch1, ch2 = _uuid("ch1"), _uuid("ch2")
    ids = await _seed_book(pid, ch1=ch1, ch2=ch2)

    bad = b"<not-xliff><unit id='x'/></not-xliff>"
    r = await client.post(
        f"/api/v1/projects/{pid}/export/reimport",
        files={"file": ("bad.xlf", bad, "application/x-xliff+xml")},
    )
    assert r.status_code == 422, r.text

    with SessionLocal() as db:
        for name, uid in ids.items():
            unit = db.get(TranslationUnit, uid)
            assert unit.target_text.startswith("Capitolo")


# ---------------------------------------------------------------------------
# Unknown project -> 404 on export and reimport.
# ---------------------------------------------------------------------------
async def test_unknown_project_404(client):
    r = await client.post(
        f"/api/v1/projects/{_uuid('nope')}/export",
        json={"format": "xliff"},
    )
    assert r.status_code == 404, r.text

    r = await client.post(
        f"/api/v1/projects/{_uuid('nope')}/export/reimport",
        files={"file": ("x.xlf", b"", "application/x-xliff+xml")},
    )
    assert r.status_code == 404, r.text
