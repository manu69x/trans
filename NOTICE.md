# Third-party components

This repository includes and builds on the following third-party works.
All bundled components are used under their original licenses; this
project's MIT license (see `LICENSE`) applies only to the original code
written for Trans.

## BookNLP (vendored, modified)

- Location: `vendor/booknlp-src/`
- Upstream: https://github.com/davidbamman/booknlp
- License: MIT — see `vendor/booknlp-src/LICENSE`
- Notes: vendored snapshot (v1.0.7 lineage) adapted to run as a local,
  GPU-capable service for literary NLP (character / quotation / coreference
  extraction, see `docs/adr/ADR-008-booknlp-gpu-service.md`). The vendored
  language data files (e.g. Project Gutenberg-derived gender terms) retain
  their original public-domain / CC licensing from the upstream project.

## DejaVu fonts (bundled)

- Location: `backend/backend/export/fonts/`
- Upstream: https://dejavu-fonts.github.io/
- License: DejaVu Fonts license (Bitstream Vera license + public domain
  additions), a free and permissive font license that permits bundling and
  embedding in produced documents. The fonts are bundled to make the PDF /
  EPUB export pipeline work out of the box without system font dependencies.
