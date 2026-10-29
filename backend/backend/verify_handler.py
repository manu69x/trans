"""Verifica massiva delle traduzioni con Open-QE (§10.2-bis, 2026-09-22).

Per ogni segmento con traduzione (bozza o approvata) il modello Open-QE
DeBERTa-v3-large (GPU0 via llama-swap, ADR-008 pattern) risponde a domande
chiuse e Trans ne memorizza le probabilita' sui campi:

* ``is_target_lang`` -- p(yes) a  "Is this text written in <target>?"
  (prima ``is_italian``: fino al multilingua 2026-10-29 la coppia era
  cablata EN→IT)
* ``is_translated``  -- p(yes) a  "Is the following text the <target>
  translation of this text?"
* ``is_source_lang`` -- p(yes) a  "Is this text written in <source>?"
  (prima ``is_english``)

L'analisi usa il testo ridotto (prima + ultima frase, delimitatore punto:
:func:`head_tail_sentences`), non il segmento intero.

La verifica non modifica mai il testo: solo misura e registra.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import Session

from .config import QE_BASE_URL  # noqa: F401 (documentazione della catena)
from .qe_client import verify_segment_qe
from .models import Job, TranslationUnit


def head_tail_sentences(text: str) -> str:
    """Il testo ridotto passato a QE: prima + ultima frase.

    Regola (2026-09-23): il delimitatore di frase e' il punto. Se il testo
    contiene almeno due frasi si usano la prima e l'ultima (giustapposte con
    uno spazio); con una sola frase o senza punti si prende tutto il segmento.
    La stessa regola e' implementata nel frontend (lib/qe.ts) per
    evidenziare esattamente il testo analizzato.
    """
    t = (text or "").strip()
    if not t:
        return ""
    parts = [p.strip() for p in t.split(".") if p.strip()]
    if len(parts) <= 1:
        return t
    head = parts[0] + "."
    tail = parts[-1] + ("." if t.endswith(".") else "")
    return f"{head} {tail}"


def run_verify_translations(db: Session, job: Job) -> None:
    """Verifica QE per ogni segmento tradotto del progetto (§10.2-bis).

    Per ogni segmento con ``target_text`` compila:
    * ``is_target_lang`` -- p(yes) a  "Is this text written in <target>?"
      (multilingua 2026-10-29: prima ``is_italian``, coppia cablata EN→IT)
    * ``is_translated``  -- p(yes) a  "Is the following text the <target>
      translation of this text?"
    * ``is_source_lang`` -- p(yes) a  "Is this text written in <source>?"

    L'analisi non usa mai il segmento intero (limite 512 token del modello):
    per sorgente e target si passa prima + ultima frase, con il punto come
    delimitatore; un segmento senza piu' frasi viene passato intero.
    """
    from .models import Project
    from .qe_client import health as qe_health

    project_id = job.payload["project_id"]
    project = db.get(Project, project_id)
    source_lang = getattr(project, "source_language", None) or "en"
    target_lang = getattr(project, "target_language", None) or "it"

    try:
        info = qe_health()
    except Exception:  # noqa: BLE001
        info = {}
    job.result = {"verify": {"phase": "start", "service": info or None}}

    units = db.query(TranslationUnit).filter(
        TranslationUnit.project_id == project_id)
    wanted = job.payload.get("segment_ids") or []
    if wanted:
        # Verifica mirata: solo i segmenti selezionati (tasto bulk frontend).
        units = units.filter(TranslationUnit.id.in_(wanted))
    units = units.filter(
        TranslationUnit.target_text.isnot(None),
        TranslationUnit.target_text != "").order_by(
        TranslationUnit.chapter_id, TranslationUnit.ordinal).all()
    total = len(units)
    done = 0
    errors = 0
    job.result = {"verify": {"phase": "run", "done": 0, "total": total}}

    for idx, u in enumerate(units, start=1):
        source_rid = head_tail_sentences(u.source_text or "")
        target_rid = head_tail_sentences(u.target_text or "")
        try:
            # Retry con backoff: il primo tentativo puo' prendere il
            # cold-load del modello Spark (502 di llama-swap mentre
            # llama-server si carica, incidente 2026-10-01 ore ~4: la
            # verifica "completed" con errors=1 e punteggi mai aggiornati).
            res = None
            for attempt in range(3):
                try:
                    res = verify_segment_qe(
                        source_rid, target_rid,
                        source_lang=source_lang, target_lang=target_lang)
                    break
                except Exception:  # noqa: BLE001 - riprova sul 502/timeout
                    if attempt < 2:
                        time.sleep(15 * (attempt + 1))
            if res is None:
                raise RuntimeError("spark verifier failed after 3 attempts")
            u.is_target_lang = float(res["is_target_lang"])
            u.is_translated = float(res["is_translated"])
            u.is_source_lang = float(res["is_source_lang"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            # Un errore del servizio NON deve azzerare una verifica
            # precedente: i valori esistenti restano finche' una nuova
            # chiamata riuscita non li sovrascrive.
            errors += 1
            if errors <= 3:
                job.result = {**(job.result or {}), "last_error": str(exc)[:200]}
        u.updated_at = datetime.utcnow()
        job.result = {**(job.result or {}),
                      "verify": {"done": idx, "total": total, "errors": errors}}
        if idx % 5 == 0 or idx == total:
            db.commit()
    if errors and done == 0:
        # Nessun segmento verificato = il servizio non era raggiungibile:
        # il job DEVE risultare failed, altrimenti l'UI mostra "completed"
        # e l'utente non capisce perche' i punteggi non cambiano.
        job.status = "failed"
        job.error = ("verifica QE non disponibile: nessun segmento verificato "
                     f"su {total} (ultime risposta: {str(job.result)[:300]})")
        db.commit()
        return
    job.result = {**(job.result or {}),
                  "verify_done": done, "verify_total": total,
                  "verify_errors": errors}


def _qe_health_unused():
    return None
