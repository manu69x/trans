"""Reset delle copie EN residue (fase 1a, 2026-09-30).

Azzera target/status/QE dei segmenti con target == source NON esenti
(>=4 parole, non numerici). SOLO reset: l'enqueue avviene via HTTP API
(`POST /translation/run`) perche' l'InProcessScheduler avvia i thread nel
processo chiamante — via `docker exec` i thread morirebbero con il processo
exec. Uso: docker exec trans-backend python3 /app/backend/repair_copies.py
"""
from __future__ import annotations

from backend.db import SessionLocal
from backend.models import TranslationUnit
from backend.translation.validators import _normalized_for_compare as nrm
from backend.translation.validators import copy_check_exempt


def main() -> None:
    db = SessionLocal()
    try:
        from backend.models import Project

        project = db.query(Project).order_by(Project.created_at).first()
        pid = str(project.id)
        units = db.query(TranslationUnit).filter(
            TranslationUnit.project_id == pid).all()
        suspects = []
        for u in units:
            if u.status == "approved":
                continue  # §5.1: mai toccati
            if nrm(u.target_text or "") != nrm(u.source_text or ""):
                continue
            if copy_check_exempt(u.source_text or ""):
                continue  # titoli brevi / ISBN: copia legittima
            suspects.append(u)
        print(f"copie sospette da riparare: {len(suspects)}")
        for u in suspects:
            print(f"  reset segmento {u.numero} ({len(u.source_text)} car)")
            u.target_text = None
            u.status = "untranslated"
            u.is_italian = None
            u.is_translated = None
            u.is_english = None
        db.commit()
        print(f"RESET OK: {len(suspects)} segmenti")
        print("IDS=" + ",".join(str(u.id) for u in suspects))
    finally:
        db.close()


if __name__ == "__main__":
    main()
