"""Bulk translation verification with the QE service (PRD §10.2-bis).

For every segment that has a translation (draft or approved) the QE
endpoint answers closed questions and Trans stores the probabilities in:

* ``is_italian``    -- p(yes) to "Is this text written in Italian?"
* ``is_translated`` -- p(yes) to "Is the following text the Italian
  translation of this text?"
* ``is_english``    -- p(yes) to "Is this text written in English?"

The analysis uses a reduced text (first + last sentence, period as the
delimiter: :func:`head_tail_sentences`), never the whole segment.

Verification never modifies the text: it only measures and records.
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import Session

from .qe_client import verify_segment_qe
from .models import Job, TranslationUnit


def head_tail_sentences(text: str) -> str:
    """The reduced text passed to the QE service: first + last sentence.

    The sentence delimiter is the period. If the text contains at least
    two sentences the first and last are used (joined with a space); with
    a single sentence or no periods the whole segment is passed. The same
    rule is implemented in the frontend (``lib/qe.ts``) so the UI can
    highlight exactly the text that was analysed.
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
    """QE verification for every translated segment of the project.

    For each segment with a ``target_text`` fills:

    * ``is_italian``    -- p(yes) to "Is this text written in Italian?"
    * ``is_translated`` -- p(yes) to "Is the following text the Italian
      translation of this text?"
    * ``is_english``    -- p(yes) to "Is this text written in English?"

    The whole segment is never sent (small context budget on the QE
    endpoint): source and target are reduced to first + last sentence;
    a segment with no further sentences is passed whole.
    """
    from .models import Project
    from .qe_client import health as qe_health

    project_id = job.payload["project_id"]
    project = db.get(Project, project_id)
    _ = project  # loaded for symmetry with the other runners

    try:
        info = qe_health()
    except Exception:  # noqa: BLE001
        info = {}
    job.result = {"verify": {"phase": "start", "service": info or None}}

    units = db.query(TranslationUnit).filter(
        TranslationUnit.project_id == project_id)
    wanted = job.payload.get("segment_ids") or []
    if wanted:
        # Targeted verification: only the selected segments (bulk action).
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
            # Retry with backoff: the first attempt may hit the cold-load
            # window of the QE model (502/timeout while the server loads).
            res = None
            for attempt in range(3):
                try:
                    res = verify_segment_qe(source_rid, target_rid)
                    break
                except Exception:  # noqa: BLE001 - retry on 502/timeout
                    if attempt < 2:
                        time.sleep(15 * (attempt + 1))
            if res is None:
                raise RuntimeError("QE verifier failed after 3 attempts")
            u.is_italian = float(res["is_italian"])
            u.is_translated = float(res["is_translated"])
            u.is_english = float(res["is_english"])
            done += 1
        except Exception as exc:  # noqa: BLE001
            # A service error must NOT wipe a previous verification: the
            # existing values stay until a new successful call overwrites
            # them.
            errors += 1
            if errors <= 3:
                job.result = {**(job.result or {}), "last_error": str(exc)[:200]}
        u.updated_at = datetime.utcnow()
        job.result = {**(job.result or {}),
                      "verify": {"done": idx, "total": total, "errors": errors}}
        if idx % 5 == 0 or idx == total:
            db.commit()
    if errors and done == 0:
        # Zero verified segments = the service was unreachable: the job
        # MUST end up failed, otherwise the UI shows "completed" and the
        # user cannot tell why the scores never changed.
        job.status = "failed"
        job.error = ("QE service unavailable: no segment verified "
                     f"out of {total} (last result: {str(job.result)[:300]})")
        db.commit()
        return
    job.result = {**(job.result or {}),
                  "verify_done": done, "verify_total": total,
                  "verify_errors": errors}
