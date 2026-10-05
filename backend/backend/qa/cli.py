"""CLI for the QA suite (AC1: "suite deterministica esegue vda CLI").

    python -m backend.qa.cli --project <id> [--chapter <id>]
                             [--status machine_draft]
                             [--critic deterministic|llm]
                             [--json]

Runs the deterministic, QE and critic levels over a chapter (or a status
slice) and prints the issues in a reproducible, human-readable form (or JSON
for scripting). Every run is audited (§13.1).
"""
from __future__ import annotations

import argparse
import json
import sys


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trans-qa",
        description="Run the QA suite (deterministic + QE + critic) "
                    "over a Trans project/chapter (§10.2 / §10.3).")
    p.add_argument("--project", required=True, help="project UUID")
    p.add_argument("--chapter", default=None,
                   help="restrict to one chapter UUID (default: all)")
    p.add_argument("--status", default=None,
                   help="restrict to translation_units.status "
                        "(default: machine_draft, untranslated)")
    p.add_argument("--critic", default="deterministic",
                   choices=["deterministic", "llm"],
                   help="critic backend (default: deterministic)")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of a readable report")
    return p


def _status_tuple(value: str | None) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(s.strip() for s in value.split(",") if s.strip())


def main(argv: list[str] | None = None) -> int:
    from backend.db import SessionLocal
    from backend.models import (
        Project,
        QaIssue,
        TranslationUnit,
    )

    from .runner import run_qa_for_project

    args = _build_parser().parse_args(argv)
    status = _status_tuple(args.status) or ("machine_draft", "untranslated")

    db = SessionLocal()
    try:
        project = db.get(Project, args.project)
        if project is None:
            print(f"project {args.project} not found", file=sys.stderr)
            return 2
        # The run itself is scoped to the chapter (AC1 "esegue su capitolo"):
        # the suite evaluates exactly this chapter's segments, so a re-run
        # reproduces the same issue set instead of accumulating (idempotent).
        summary = run_qa_for_project(
            db, args.project, critic_backend=args.critic,
            unit_status=status, chapter_id=args.chapter)

        # Fetch the issues for the report. When a chapter is selected restrict
        # to it; always join translation_units so the report can be ordered by
        # segment ordinal.
        issue_query = db.query(QaIssue).filter(
            QaIssue.project_id == args.project).join(
            TranslationUnit, QaIssue.unit_id == TranslationUnit.id)
        if args.chapter:
            issue_query = issue_query.filter(
                TranslationUnit.chapter_id == args.chapter)
        # Deterministic ordering (stable across re-runs): segment ordinal,
        # then category + evidence, then severity. Not by created_at (fresh
        # timestamps every run would reorder the report).
        issues = issue_query.order_by(
            TranslationUnit.ordinal, QaIssue.category, QaIssue.evidence,
            QaIssue.severity).all()

        if args.json:
            print(json.dumps({
                "project_id": args.project,
                **summary,
                "qe_scores": summary["qe_scores"],
                "issues": [
                    {
                        "segment_id": str(i.unit_id),
                        "category": i.category,
                        "severity": i.severity,
                        "kind": i.kind,
                        "evidence": i.evidence,
                        "message": i.message,
                        "suggestion": i.suggestion,
                        "resolved": i.resolved,
                    }
                    for i in issues
                ],
            }, ensure_ascii=False, indent=2))
        else:
            print(f"# QA report — project {args.project}")
            print(f"# segments evaluated : {summary['segments_evaluated']}")
            print(f"# deterministic issues: {summary['deterministic_issues']}")
            print(f"# critic issues      : {summary['critic_issues']}")
            print(f"# issues persisted   : {summary['issues_persisted']}")
            print("# QE scores:")
            for sid, sc in summary["qe_scores"].items():
                print(f"  {sid}: {sc}")
            print(f"# {len(issues)} issues:")
            for i in issues:
                print(f"  [{i.severity}] {i.category} "
                      f"(unit {i.unit_id}): {i.message}")
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI surfaces the error
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
