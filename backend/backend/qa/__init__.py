"""QA subsystem (PRD §10.2 / §10.3 / §12.4 / §18.2).

Three levels, each a *signal* (never a single gate §10.3 / §17):

* :mod:`~backend.qa.deterministic` -- the §10.2 deterministic suite;
* :mod:`~backend.qa.quality_estimation` -- reference-free QE (local) §10.3;
* :mod:`~backend.qa.critic` -- the LLM critic, schema-validated (§10.3 / AC3).

:mod:`~backend.qa.runner` runs all three for a project and persists them;
:mod:`~backend.qa.cli` exposes the same from the command line (AC1).
"""
from __future__ import annotations

from . import categories as C
from .critic import CRITIC_SCHEMA, CriticResult, run_critic_deterministic
from .deterministic import run_deterministic
from .quality_estimation import (
    QEScore,
    calibrate,
    estimate_segment_quality,
)
from .runner import run_qa_for_project

__all__ = [
    "CRITIC_SCHEMA",
    "C",
    "CriticResult",
    "QEScore",
    "calibrate",
    "estimate_segment_quality",
    "run_critic_deterministic",
    "run_deterministic",
    "run_qa_for_project",
]
