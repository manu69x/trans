#!/usr/bin/env python3
"""One-shot: point the benchmark at a throwaway DB and run it.

The ``vector`` extension is superuser-only on this box, so the benchmark
must never DROP SCHEMA public (it would remove the pgvector type). The
benchmark itself now only drops/recreates TABLES, leaving the extension
in place; any of the pgvector-capable test databases works. We use
``trans_test`` (a test database, wiped and rebuilt on every run).
"""
import os
import pathlib
import sys

os.environ["DATABASE_URL"] = "postgresql://trans:trans@127.0.0.1:5432/trans_test"
print("using throwaway DB: trans_test (tables wiped and rebuilt)")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bench_ner_llm  # noqa: E402
raise SystemExit(bench_ner_llm.main())
