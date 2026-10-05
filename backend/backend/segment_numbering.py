"""Numerazione progressiva dei segmenti per progetto (§11.2, 2026-09-22).

Ogni segmento riceve alla creazione un ``numero`` unico e stabile, mostrato
in tutte le schede che elencano i segmenti (Segmenti, Traduzione, QA).
"""
from sqlalchemy import func, text

from .models import TranslationUnit


def next_numero_start(db, project_id: str) -> int:
    """Il primo numero disponibile nel progetto (max assegnato + 1).

    Prende un lock di riga sulla riga del progetto: due job che creano
    segmenti in parallelo sullo stesso progetto leggerebbero lo stesso
    ``max(numero)`` e violerebbero ``ux_tu_project_numero``. Il lock resta
    fino al commit/rollback della sessione e serializza solo la fase di
    assegnazione.
    """
    db.execute(
        text("SELECT id FROM projects WHERE id = :pid FOR UPDATE"),
        {"pid": str(project_id)},
    )
    return (
        db.query(func.max(TranslationUnit.numero))
        .filter(TranslationUnit.project_id == project_id)
        .scalar()
        or 0
    ) + 1
