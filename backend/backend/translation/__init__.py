"""F3 translation subsystem (PRD §5.4, §7.2-7.3, §9.3-9.4, §10.1).

Pure, dependency-light building blocks for the translation planner:

* :mod:`~backend.translation.embedding` -- deterministic local sentence
  embeddings (no external model) so the pgvector semantic retrieval (§7.2)
  can run fully offline.
* :mod:`~backend.translation.tm_retrieval` -- TM retrieval (exact -> fuzzy
  -> semantic) with the mandatory project filter and configurable threshold
  (§7.2).
* :mod:`~backend.translation.planner` -- the block planner: real-token
  budget (:class:`TokenBudget`, §10.1 step 1), prompt assembly with
  immutable snapshots (§9.4/§10.1 step 3), instruction hierarchy and
  conflict exposure (§9.3).
"""
from __future__ import annotations
