# Benchmarks

Local harnesses that measure the quality of the extraction and entity
pipelines against a synthetic [corpus](corpus/README.md). They exist so
that parser/OCR/NER changes can be judged with numbers instead of
impressions — everything runs locally, no cloud services.

| Harness | What it measures | How to run |
|---------|------------------|------------|
| `corpus/` + `corpus/generate_corpus.py` | The 9-PDF gold corpus (5 native, 4 synthetic scans), regenerable deterministically | see [corpus/README.md](corpus/README.md) |
| `tools/bench_parsers.py` (repo root `tools/`) | L1 extraction quality across the corpus | `python tools/bench_parsers.py --help` |
| `tools/bench_ocr.py` (repo root `tools/`) | OCR CER/WER on the synthetic scans (standard LCS alignment), escalation behaviour L3→L4 | `python tools/bench_ocr.py --help` |
| `bench_ner_llm.py` (this folder) | LLM NER entity extraction: schema compliance, candidate quality vs the fantasy chapter gold | `python _run_bench_nerllm.py` (targets the throwaway `trans_test` DB) |

## Notes

- Results are not committed: they depend on your models and hardware. Run
  the harnesses on your own stack and record the numbers in your issue/PR
  when they inform a change.
- The OCR escalation logic and its calibration are described in
  [ADR-002](../adr/ADR-002-parser-pipeline.md).
- The NER benchmark needs a reachable PostgreSQL (like the test suite) and
  a configured gateway for the LLM pass; everything else runs offline.
