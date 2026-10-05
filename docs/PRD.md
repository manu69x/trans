# PRD — Local platform for professional translation of English novels into Italian

**Version:** 1.0
**Date:** 2 September 2026
**Status:** development spec (all phases implemented — see the note below)
**Intended deployment:** local / on-premise, with no manuscript data sent to external cloud services

> **Implementation status (2026-10).** This document is the original product
> specification this repository implements. Phases 0–4 (discovery, import &
> structure, entities/glossary/models, translation CAT, QA/export/hardening)
> are implemented; deviations from the letter of this spec are documented in
> the [ADRs](adr/README.md) (e.g. the in-process job scheduler instead of a
> full Celery deployment). Inline source citations were kept from the
> original research notes and are normalised to bracketed references.

---

## 1. Summary

### 1.1 Product

Build a local web platform to assist professional translators in translating
novels and essays from English into Italian. The user uploads a PDF; the
system extracts and structures the text, identifies chapters, characters and
other entities, builds a controlled glossary and a translation memory,
translates the content block-by-block with local LLM models available
through the **LLM Gateway**, then subjects the result to automatic checks
and human review.

The product must never present an LLM translation as ready for publication.
Recent research shows that literary translations published by humans keep
outperforming LLM output, which tends to be more literal and less
stylistically varied; targeted prompting and human post-editing are needed
to reach accuracy, fidelity and cultural appropriateness. The platform is
therefore an **LLM-assisted literary CAT tool**, not a "one-click" machine
translator.

### 1.2 Problem solved

Translating a novel requires global coherence over:

- names, aliases, titles and nicknames;
- characters' grammatical and referential gender;
- invented objects, places, races/species, organizations and concepts;
- register, focalisation, narrative voice and dialogue;
- already-approved translations and terminology decisions;
- genre-specific style and conventions.

LLMs translate better when they receive pertinent local lexicon, memory and
instructions. Terminology-constrained translation and a
"translate-then-refine" cycle improve terminology recall; violations can be
detected with alignment and corrected with a second constrained decoding.

### 1.3 Goals

1. Reliable import of digital PDFs and scans.
2. Identify the book's natural structure: title page, TOC, parts, chapters,
   scenes, notes, appendices.
3. Create and maintain a per-project knowledge base with entities, aliases,
   type, gender, number, translation, editorial decision and evidence.
4. Translate in blocks within a maximum working window of 16K tokens,
   preserving inter-chapter context and coherence.
5. Give the translator explicit control over glossary, style, translation
   memory, prompts, models and approvals.
6. Keep non-local data out of the flow: PDFs, text, glossaries, results and
   telemetry must not leave the local network without the administrator's
   explicit choice.
7. Export a reviewable, traceable translation as DOCX, EPUB/HTML and
   bilingual CAT files (XLIFF/TMX/CSV).

### 1.4 Non-goals for the first release

- Certified translation, or replacing the translator/publisher.
- Perfect OCR of degraded, handwritten or complex-layout PDFs without
  review.
- Automatic translation of images containing text, comics or illustrated
  plates.
- Multi-organisation collaboration and SaaS billing.
- Pixel-perfect typographic reconstruction of the original PDF.
- Training / fine-tuning of the LLM models.

---

## 2. Users and use cases

### 2.1 Roles

| Role | Permissions and responsibilities |
|---|---|
| Administrator | Configures the LLM Gateway, models, storage, authentication, quotas and backups |
| Project manager / editor | Creates projects, sets language, genre, style guides, workflow and assignments |
| Translator | Uploads PDFs, fixes structure, manages the glossary, starts translations, reviews and approves segments |
| Revisor | Compares EN/IT, annotates MQM errors, approves/rejects translations and terminology decisions |
| QA reader | Consults dashboards and reports without modifying approved text |

### 2.2 Core user stories

- As a translator, I want to upload a PDF and know whether it is native text
  or an OCR scan, with a per-page confidence score.
- As a translator, I want to see and correct part/chapter/scene boundaries
  before translation starts.
- As a translator, I want a list of characters, places, objects and terms
  with aliases and textual examples, so I can approve decisions before bulk
  translation.
- As a translator, I want to choose separately the text/analysis model and
  the translation model among those exposed by the LLM Gateway.
- As a translator, I want to choose the text type — science fiction, horror,
  romance, essay — to apply the appropriate style guide and prompts.
- As a translator, I want to work segment by segment with source, proposed
  translation, memory, glossary and evidence on screen.
- As a revisor, I want to mark accuracy, terminology, style, register,
  grammar and omission errors with severity.
- As a project manager, I want to export only approved segments and keep a
  complete audit trail.

---

## 3. Product principles

1. **Mandatory human-in-the-loop.** The user can start automatic batches,
   but output stays "draft" until approved.
2. **Evidence-first.** Every genre, alias, termbase translation and
   suggestion must be able to show the source passage that justifies it.
3. **Glossary before translation.** Do not start a chapter batch without
   glossary, style and memory snapshots.
4. **Selective context, not indiscriminate context.** The 16K window must be
   allocated to current text, adjacent context and targeted entity/TM
   retrieval.
5. **Immutability of approved versions.** Changing a term or a translation
   produces a new version and selectively invalidates affected outputs.
6. **Local by default.** Storage, database, models and APIs stay local.
7. **Reproducibility.** Every output preserves model, version,
   quantisation (if known), parameters, prompt template, text hash, and
   termbase/TM snapshots.

---

## 4. Logical architecture

```text
Web browser
  └── Frontend (React/Next.js or equivalent)
        └── Application API (FastAPI/Node)
              ├── PostgreSQL + pgvector
              ├── Local object storage (PDFs, assets, exports)
              ├── Job queue (Redis + Celery/RQ or equivalent)
              ├── Parsing/OCR worker
              ├── NLP worker (BookNLP + LLM)
              ├── Translation/QA worker
              └── LLM Gateway (OpenAI-compatible endpoint, local models)
                    ├── analysis/text model
                    └── translation model
```

### 4.1 Components

| Component | Responsibility |
|---|---|
| Frontend | Upload, bilingual editor, glossary, model configuration, review, export |
| API | Authorisation, orchestration, REST/WebSocket/SSE, validation and audit |
| Job queue | Long, repeatable jobs: OCR, parsing, chunking, extraction, translation, QA |
| Parser/OCR | Structured PDF extraction with OCR fallback and page coordinates |
| NLP | NER, name clustering, coreference, classification and entity proposals |
| Local RAG | Retrieves termbase, characters, style guide, memory and relevant context |
| Gateway adapter | Reads available models, runs chat/completions, streaming, health checks |
| QA | Deterministic validations, quality estimation, LLM review and human checks |
| Persistence | Projects, segments, revisions, assets, versioned prompts and results |

### 4.2 Suggested stack

- **Frontend:** React + TypeScript + Next.js, Tailwind, TanStack Query,
  tiptap/ProseMirror editor.
- **Backend:** Python FastAPI for natural access to parsers, OCR, BookNLP,
  quality estimation and NLP libraries.
- **Database:** PostgreSQL 16+, `pgvector`, JSONB; encrypted filesystem or
  local MinIO for binary assets.
- **Queue:** Redis + Celery/RQ; idempotent jobs with controlled retry.
- **LLM inference:** exclusively the LLM Gateway; an internal
  OpenAI-compatible adapter avoids provider lock-in.
- **Local observability:** OpenTelemetry + sanitised JSON logs; no
  manuscript payload in logs.
- **Deployment:** Docker Compose for the MVP; Kubernetes optional for
  multi-GPU installations.

---

## 5. End-to-end flow

### 5.1 Project state

```text
DRAFT → IMPORTING → PARSED → STRUCTURE_REVIEW → ENTITY_REVIEW
→ READY_FOR_TRANSLATION → TRANSLATING → QA_REVIEW → APPROVED → EXPORTED
```

Every state allows going back to a previous one; a structural or
terminological change invalidates only the dependent jobs, not the entire
project.

### 5.2 PDF import

1. The user creates a project and uploads a PDF.
2. The system computes the SHA-256 hash, size, page count and metadata.
3. It determines per page whether a reliable text layer exists.
4. Digital PDFs get layout-aware extraction; scans get OCR preserving
   bounding boxes, page, confidence and reading order.
5. Both the normalised text and the "raw extraction" with coordinates are
   kept, so the original evidence can be located.
6. A report is shown: successful pages, OCR pages, percentage of doubtful
   characters, repeated header/footer text, columns and pages to verify.

**Technical choice:** adopt a layered chain, not a single parser:

- Level 1: PyMuPDF/pdfplumber for native text, metadata, bookmarks and
  coordinates.
- Level 2: Docling for digital-born PDFs and local structured-markdown
  conversion; a lightweight, Markdown-first solution for native PDF
  extraction.
- Level 3: `olmOCR` for scanned/difficult PDFs; open source and designed to
  produce clean structured text from PDFs and images.
- Level 4: PaddleOCR/PDF-Extract-Kit for OCR + layout detection;
  PDF-Extract-Kit includes text, title, layout detection and OCR via
  PaddleOCR.

The parser must preserve reading order and hierarchy: layout detection
exists precisely to identify blocks, paragraphs, headings, images, tables
and reading order.

### 5.3 Structure and chapter detection

#### Evidence sources, in order of reliability

1. PDF outline/bookmarks (`get_toc()` when present).
2. Textual table of contents recognised in the first pages.
3. Typographic headings: font size, weight, caps, alignment, spacing
   before/after, Roman/Arabic numerals.
4. Configurable lexical patterns: `CHAPTER`, `Chapter`, `CH.`, `PART`,
   `BOOK`, `PROLOGUE`, `EPILOGUE`, `INTERLUDE`, Roman numerals, isolated
   titles.
5. Page breaks and narrative signals: separators, asterisks, datelines,
   place/time.
6. LLM verification only for ambiguous cases, using extracts and
   coordinates; never delegate primary segmentation to the model.

PyMuPDF can extract the PDF outline only when embedded; without a TOC, the
heuristic on font sizes larger than body text is useful but not sufficient.
A visual editor to create, merge, split, move and rename chapters must
therefore always exist.

#### Structure output

```json
{
  "node_id": "uuid",
  "parent_id": "uuid|null",
  "kind": "front_matter|part|chapter|scene|back_matter|footnote",
  "source_label": "CHAPTER III",
  "normalized_title": "Chapter III",
  "start_page": 21,
  "end_page": 37,
  "start_char": 18293,
  "end_char": 47620,
  "confidence": 0.96,
  "detection_method": ["pdf_toc", "font", "regex"],
  "status": "proposed|user_confirmed"
}
```

#### Cleaning rules

- Remove repeated headers/footers only after algorithmic confirmation over
  at least N pages and with a rollback preview.
- Do not join hyphen-broken words when the hyphen is semantically
  meaningful.
- Preserve em dashes, ellipses, quotation marks, italics and emphatic caps
  for literary style.
- Extract notes, epigraphs and letters as distinct nodes, since they need
  different prompts/registers.
- Never auto-translate title page, copyright, TOC, ISBN, acknowledgements or
  editorial notes without explicit selection.

### 5.4 Chunking

#### Terminology

- **Chapter:** the natural editorial unit.
- **CAT segment:** the minimum editable/aligned unit (sentence or paragraph,
  with dialogue exceptions).
- **LLM block:** the group of segments sent to the LLM Gateway for
  translation.
- **Context:** text not to be translated in the batch but provided for
  coherence.

#### Rules

1. The chapter is the primary unit of progress, glossary and QA.
2. First segmentation: paragraphs, dialogue, epigraphs, letters, quotes and
   scene breaks.
3. Second segmentation: sentences via an English parser, without breaking:
   - dialogue with speech tags;
   - a sentence with an unclosed parenthesis or quote;
   - a list or a poem;
   - text with markup/placeholders;
   - a multi-token proper noun.
4. Maximum LLM block: **16,384 total tokens**, not 16 KB of bytes.
5. Initial budget: 10,000–11,000 source tokens, 2,000–2,500 tokens of
   glossary/TM/context, 2,000–3,000 tokens reserved for output and
   reasoning. Real limits must be computed with the tokenizer exposed by
   the selected model.
6. Overlap: 1–2 previous segments and 1 following segment as "read-only"
   context, explicitly labelled `DO_NOT_TRANSLATE_CONTEXT`.
7. Block content is sent in order, with stable per-segment IDs; the response
   must return the same IDs.
8. The system verifies that IDs, tags and placeholders are not missing,
   duplicated or reordered.

A single huge context does not guarantee uniform narrative comprehension:
long-context fiction comprehension degrades measurably (cf. Fiction.liveBench).
A chapter-based architecture with small blocks and selective retrieval is
therefore preferable to loading the whole book in one call.

### 5.5 English preprocessing

#### General principle

The original text and the normalised text must coexist. No destructive
transformation may replace the source. Every modified token gets a
reversible transformation map.

#### Saxon genitive `'s`

User requirement: insert a space between the name and `'s` to ease
translation.

**Product decision:** do not apply the blind substitution `X's → X 's`.
The English apostrophe+s can mean possessive, `is`, `has` or `us` (in
`let's`); research indicates POS/contextual disambiguation is required
before transforming.

Pipeline:

1. POS/dependency parse of the English text.
2. Normalise only when `'s` is classified possessive (`POS`) and the
   possessor is a proper noun or noun phrase.
3. Insert reversible internal control tokens, e.g. `[[POSS:John]] 's`, not
   a plain visible space.
4. In the prompt, explain that the construction is an English possessive to
   be rendered in natural Italian (`il libro di John`, `la sua stanza`,
   ...), not by copying the English form.
5. After translation, remove the control tokens and validate that none
   remain in the final text.

Examples:

| Source | Internal normalisation | Interpretation |
|---|---|---|
| `Mary's coat` | `[[POSS:Mary]] 's coat` | possessive |
| `Mary's late` | unchanged | `Mary is late` |
| `Mary's been here` | unchanged | `Mary has been here` |
| `Let's go` | unchanged | `let us` |

#### The pronoun `I`

Hard deterministic rule:

- the exact token `I`, isolated by punctuation/whitespace and parsed as a
  personal pronoun (`PRP`), **is never a proper name**;
- it never enters the termbase or the name list;
- it may be included in the grammatical context to preserve first person,
  register and agreement;
- Roman names or acronyms containing `I` as part of different tokens
  (`Icarus`, `Unit I`, `I-5`) remain valid if detected by the parser.

#### Other protected normalisations

- Unicode NFC and controlled normalisation of typographic
  quotes/apostrophes.
- Preservation of italics, emphatic caps, non-breaking spaces and poetic
  line breaks.
- Placeholders for URLs, emails, numbers, dates, quotes, notes and rich
  text tags.
- Scan of hyphen-broken words at line ends with review for ambiguous cases.
- Recognition of dialogue and speaker attribution where possible.

---

## 6. Entity extraction and management

### 6.1 Rationale

Generic NER is insufficient for novels: aliases, titles, partial names and
coreferences must be resolved. BookNLP is specifically designed for English
books and long documents; it includes entity recognition, name clustering
(e.g. Tom / Tom Sawyer / Mr. Sawyer), coreference, quote attribution,
supersense, events and referential-gender inference through pronouns. Its
average coreference is however 76.4–79.0 F1, so proposals must be
reviewable, not definitive.

### 6.2 NER/coreference pipeline

1. BookNLP over the whole chapter — or the whole text when resources allow.
2. Additional transformer NER for organizations, places, works and
   artifacts not well covered.
3. Structured LLM pass to classify only high-ambiguity candidates and
   domain categories (alien species, artifacts, curses, fictional
   institutions).
4. Internal entity linking: alias unification via rules + coreference +
   user confirmation.
5. Entity states: `proposed`, `verified`, `approved`, `deprecated`,
   `merged`.
6. Every entity keeps mentions, counts, pages, chapters, evidence,
   confidence and provenance.

### 6.3 Entity categories

| Category | Examples | Relevant properties |
|---|---|---|
| `PERSON` | Anna, Mr. Darcy, The Doctor | referential gender, number, pronouns, titles, aliases |
| `ROLE` | the Captain, the Widow, Detective Hale | gender/number if determinable, possible link to a person |
| `CREATURE_SPECIES` | vampire, Wyrm, Martian | number, IT grammatical gender, canonical translation |
| `OBJECT_ARTIFACT` | Black Key, Time Engine | IT grammatical gender, number, translatability |
| `LOCATION` | London, Ravenwood Manor | translatability, article, canonical Italian form |
| `ORG_FACTION` | The Order, NASA | article, acronym, translatability |
| `WORK_MEDIA` | The Book of Ashes | title/work policy, italics |
| `EVENT` | The Fall, Winter War | translatability, article |
| `CONCEPT_TERM` | the Veil, resonance | IT grammatical gender, definition, preferred/forbidden |
| `TITLE_HONORIFIC` | Sir, Lady, Dr., Captain | rendering strategy, agreement |

### 6.4 Distinguishing sex, referential gender and grammatical gender

The database must not have a single ambiguous `gender` field. It must use
at least:

- `referential_gender`: `male | female | nonbinary | mixed | unknown | not_applicable`;
- `referential_gender_evidence`: pronouns/quotes and confidence;
- `italian_grammatical_gender`: `masculine | feminine | common | variable | not_applicable`;
- `grammatical_number`: `singular | plural | invariant | collective | unknown`;
- `translation_policy`: `keep_source | translate | transliterate | contextual | undecided`.

BookNLP infers referential gender from the pronouns used for the character,
not personal identity; the UI and prompts must respect this distinction.
For non-person entities, the "gender" used in translation is the Italian
grammatical gender, which must not be inferred from sex or English form.

### 6.5 Essential database schema

```sql
CREATE TABLE projects (
  id UUID PRIMARY KEY,
  title TEXT NOT NULL,
  source_language TEXT NOT NULL DEFAULT 'en',
  target_language TEXT NOT NULL DEFAULT 'it',
  genre_profile TEXT NOT NULL,
  translation_model_id TEXT,
  text_model_id TEXT,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE entities (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  canonical_source TEXT NOT NULL,
  canonical_target TEXT,
  entity_type TEXT NOT NULL,
  referential_gender TEXT NOT NULL DEFAULT 'unknown',
  italian_grammatical_gender TEXT NOT NULL DEFAULT 'not_applicable',
  grammatical_number TEXT NOT NULL DEFAULT 'unknown',
  translation_policy TEXT NOT NULL DEFAULT 'undecided',
  definition TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'proposed',
  confidence NUMERIC(4,3),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE entity_aliases (
  id UUID PRIMARY KEY,
  entity_id UUID NOT NULL REFERENCES entities(id),
  source_alias TEXT NOT NULL,
  target_alias TEXT,
  alias_type TEXT NOT NULL,
  UNIQUE(entity_id, source_alias)
);

CREATE TABLE entity_evidence (
  id UUID PRIMARY KEY,
  entity_id UUID NOT NULL REFERENCES entities(id),
  chapter_id UUID,
  source_segment_id UUID,
  page_number INT,
  quote_text TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  confidence NUMERIC(4,3),
  extractor TEXT NOT NULL
);

CREATE TABLE glossary_terms (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  source_term TEXT NOT NULL,
  target_term TEXT,
  term_type TEXT NOT NULL,
  preferred BOOLEAN NOT NULL DEFAULT true,
  forbidden_targets JSONB NOT NULL DEFAULT '[]',
  grammatical_gender_it TEXT,
  grammatical_number TEXT,
  inflection_notes TEXT,
  usage_notes TEXT,
  status TEXT NOT NULL,
  version INT NOT NULL DEFAULT 1
);

CREATE TABLE translation_units (
  id UUID PRIMARY KEY,
  project_id UUID NOT NULL REFERENCES projects(id),
  chapter_id UUID,
  ordinal INT NOT NULL,
  source_text TEXT NOT NULL,
  target_text TEXT,
  status TEXT NOT NULL DEFAULT 'untranslated',
  source_hash TEXT NOT NULL,
  glossary_snapshot_id UUID,
  tm_snapshot_id UUID,
  model_run_id UUID,
  quality_score NUMERIC(5,2),
  UNIQUE(project_id, chapter_id, ordinal)
);
```

(The implemented schema extends this baseline: see
[ADR-003](adr/ADR-003-db-schema.md) for the full ER diagram, indexes,
TM embeddings and the later revisions.)

### 6.6 Entity UI workflow

- Table filterable by state, type, gender, number and chapter.
- Side panel with all mentions and ±2-paragraph extracts.
- Manual merge/split of aliases.
- "Approved Italian form" field.
- "Never translate" and "allow Italian inflection" checkboxes.
- "Forbidden forms" field.
- Priority setting: block the batch or just warn when a term is uncertain.
- Show entities introduced in a chapter and changes vs the previous chapter.

---

## 7. Translation memory, termbase and retrieval

### 7.1 TM and termbase are distinct components

The **translation memory** stores approved source-target segments; the
termbase defines preferred terms, forbidden terms and canonical
translations. Used together they control coherence: the TM reuses validated
formulations and the termbase governs critical lexicon.

### 7.2 Translation memory

For every approved segment store:

- normalised source and original source;
- approved target;
- project, chapter, scene, genre, narrator/POV when known;
- revisor, date, version and QA score;
- source embedding and metadata;
- terms used;
- project rights/permissions.

Retrieval per block:

1. exact match on the normalised source;
2. lexical fuzzy match;
3. semantic match via pgvector;
4. mandatory filter per project; optional per chapter/POV/genre;
5. include only approved matches above a configurable threshold;
6. never let the TM be copied mechanically when the narrative context is
   incompatible.

TM maintenance must detect duplicates, sources with contradictory targets,
misaligned tags/placeholders, length discrepancies and obsolete entries —
known problems in professional TM management.

### 7.3 Selecting the entities to pass to a block

Do not send the whole termbase: it consumes context and increases
conflicts. Pass only:

- entities mentioned in the block;
- entities that appeared in the two previous segments;
- entities retrieved via coreference from the chapter;
- high-priority terms with lexical overlap;
- a configurable maximum (default 30 entities + 20 terms).

For each entry include: source form, target form, type, IT grammatical
gender, number, aliases, policy, notes and priority. `unknown` entities
must not induce the model to invent a gender.

---

## 8. Models and the LLM Gateway

### 8.1 Integration requirements

The LLM Gateway is the only source of models for inference. The application
must not hardcode names/models: it must interrogate the provider's models
endpoint at startup and on explicit refresh.

The adapter must expose a capability matrix:

| Capability | Use |
|---|---|
| `id`, `display_name`, `provider` | Frontend selectors |
| `context_window` | Block and budget computation |
| `max_output_tokens` | Batch limit |
| `supports_json_schema` | Entity extraction and structured QA |
| `supports_streaming` | Live translation UX |
| `supports_reasoning` | Disabled for translation, controllable for analysis |
| `supports_seed` | Reproducibility when available |
| `languages` | EN→IT filter |
| `locality` | On-premise verification |
| `health/latency` | Disable models that are not ready |

### 8.2 Frontend selectors

The project page must show two distinct mandatory fields:

1. **Model for analysis and text work**
   - assisted OCR, disambiguation, ambiguous segmentation, LLM NER,
     classification, QA, review, chapter summary.
2. **Model for translation**
   - block translation, terminology refine, fidelity check.

Both use dropdowns populated with compatible gateway models. Details must be
shown: context window, max output, JSON support, status, VRAM/host if
exposed, and a warning when the model does not meet the required budget.

Advanced per-model settings:

- temperature (translation default 0.1–0.3; analysis 0–0.2);
- top_p;
- seed when supported;
- reasoning on/off and reasoning budget;
- timeout;
- retry count;
- max output;
- prompt version/template.

**Policy:** for literary translation, visible reasoning must not end up in
the output or become an unpredictable cost source. Output must be
schema-constrained and validated.

### 8.3 Gateway adapter contract

```ts
interface GatewayModel {
  id: string;
  displayName: string;
  contextWindow?: number;
  maxOutputTokens?: number;
  supportsJsonSchema: boolean;
  supportsStreaming: boolean;
  supportsReasoning?: boolean;
  status: 'available' | 'degraded' | 'offline';
}

interface LlmRunRequest {
  modelId: string;
  purpose: 'analysis' | 'translation' | 'qa';
  systemPrompt: string;
  userPrompt: string;
  jsonSchema?: object;
  temperature: number;
  maxOutputTokens: number;
  seed?: number;
}
```

### 8.4 Fallback and resume

- If a translation model goes offline, suspend the batch and ask the user;
  never substitute the model automatically without an audit trail.
- Retry only transient errors, with an idempotency key per block.
- Save streamed partial output as draft only if it respects schema and
  segment checksums.
- Allow replaying a block with the same configuration or with a comparable
  "branch run".

---

## 9. Genre profiles and prompts

### 9.1 Text-type selector

The selector is mandatory at project creation:

- Science fiction novel
- Horror novel
- Romance novel
- Essay

Future option: "Custom profile". The profile does not arbitrarily change
content or plot: it configures style, checks, glossary depth and prompts.

### 9.2 Profile matrix

| Profile | Translation focus | Priority glossary | Specific QA |
|---|---|---|---|
| Science fiction | worldbuilding coherence, neologisms, technology, races/species, units | artifacts, starships, ranks, factions, physics concepts | nomenclature coherence, units, acronyms, technical terms |
| Horror | tension, ambiguity, rhythm, sensory imagery, not over-explaining | places, ritual objects, creatures, occult terminology | intensity, register, omissions, repetitions, handling of the unsaid |
| Romance | voice, intimacy, dialogue, relational register, gender agreements | endearments, nicknames, family roles, clothes/settings | dialogic register, allocutive pronouns, consent, emotional coherence |
| Essay | conceptual accuracy, argumentation, quotes, disciplinary terminology | technical terms, authors, works, institutions, dates | fidelity, quotes, notes, terminological and logical coherence |

### 9.3 Instruction hierarchy

1. Hard platform rules: privacy, schema, tag preservation, no invention.
2. The project's user-approved style guide.
3. Glossary and entities with priority.
4. Approved translation memory.
5. Genre profile.
6. Chapter and block context.
7. The text to translate.

In case of conflict, the platform must surface it instead of letting the
model decide silently.

### 9.4 Base translation prompt

```text
ROLE
You are a professional editorial translator from English to Italian.
Translate with semantic fidelity, natural Italian literary style and absolute
coherence with the approved glossary. Do not summarise, do not censor, do not add
explanations, do not omit content and do not invent details.

PRIORITY
1. Preserve segment IDs, tags and placeholders exactly.
2. Apply the approved GLOSSARY/ENTITIES entries. "Do not translate" forms must
   remain identical unless inflection is explicitly allowed.
3. For PERSON use the referential gender only when provided as evidence.
   For objects/places/concepts use the provided Italian grammatical gender.
   If the datum is unknown, do not invent a gender: use a natural rendering,
   avoid assumptions when possible, and flag the doubt in the flags field.
4. The [[POSS:...]] 's markings indicate an English possessive. Render it in
   natural Italian; never emit such markings.
5. The isolated token "I" is the English first-person pronoun, never a proper name.
6. Preserve voice, tense, focalisation, dialogue rhythm and significant punctuation.
7. Context is for comprehension only: do not translate or return it.

GENRE PROFILE
{{genre_style_prompt}}

GLOSSARY AND ENTITIES
{{retrieved_approved_terms}}

TRANSLATION MEMORY (approved examples only; adapt to context)
{{tm_matches}}

PREVIOUS CONTEXT (do not translate)
{{previous_context}}

TEXT TO TRANSLATE
{{segment_batch_with_ids}}

OUTPUT
Return only JSON conforming to the schema. One item per received ID.
```

### 9.5 Translation output schema

```json
{
  "translations": [
    {
      "segment_id": "uuid",
      "target_text": "...",
      "used_entity_ids": ["uuid"],
      "term_violations": [],
      "flags": [
        {
          "type": "gender_ambiguous|term_ambiguous|ocr_suspect|source_ambiguous|other",
          "message": "..."
        }
      ]
    }
  ]
}
```

Mandatory use of JSON Schema / grammar-constrained decoding when the gateway
supports it. In llama.cpp, GBNF grammars can force valid JSON and be derived
from JSON Schema. This reduces format errors but does not replace semantic
validation.

---

## 10. Translation, validation and QA

### 10.1 Phases per block

1. Token budget computed with the real tokenizer of the chosen model.
2. Retrieval of relevant TM, glossary and entities.
3. Prompt construction with an immutable snapshot.
4. LLM translation with JSON-constrained output.
5. JSON syntactic validation.
6. Segment validation: same IDs, order, cardinality, no extra/missing IDs.
7. Placeholder/tag/protected-punctuation validation.
8. Terminology enforcement: presence/form of mandatory entries, absence of
   forbidden forms.
9. Deterministic and LLM automatic QA.
10. Save as `machine_draft`; never as `approved`.
11. Send to the bilingual editor for review and approval.

### 10.2 Deterministic checks

- Segment IDs preserved.
- Numbers, dates, units, codes and URLs preserved or transformed per policy.
- No internal `[[...]]` token in the target.
- Balanced opening/closing placeholders/tags.
- No forbidden term.
- `must_keep` terms identical.
- Entities translated per canonical target/policy.
- No translation identical to the source above threshold without a valid
  exception.
- Detection of unusually short/long output.
- Detection of duplicated, skipped or merged segments.

### 10.3 Semantic QA

Two layers:

- **Reference-free Quality Estimation:** a per-segment score to prioritise
  review. QE assesses quality without a reference and can operate at word,
  sentence, segment or document level. COMETKiwi/XCOMET or a compatible
  local model are candidates; it must be shown as a signal, not as truth.
- **Separate LLM critic:** receives source, target, applicable glossary and
  style guide. Returns structured errors without rewriting the text, with
  category, severity, evidence and suggestion.

Automatic scores must not be the only gate: even advanced metrics correlate
imperfectly with human MQM (e.g. COMET-22 ~0.69 and XCOMET ~0.72 at system
level in the cited data).

### 10.4 Human MQM annotation

The editor must allow span annotations with categories:

- Accuracy: mistranslation, omission, addition, untranslated.
- Terminology: inconsistency, wrong term, forbidden term.
- Italian language: grammar, spelling, punctuation, collocation.
- Style: awkward, register, voice, tone, calque, repetition.
- Locale/conventions: formatting, quotes, measure/date.
- Source: OCR suspected, ambiguous source.

Severity: `minor`, `major`, `critical`.

The MQM method with span, category, severity and comment annotation is more
useful than a bare automatic score because it explains which problem to fix.

### 10.5 Controlled refine

When selecting "Fix with LLM":

- send only the necessary segment/context;
- include the MQM errors and the specific constraints;
- request a replacement target and a changelog;
- show a diff before applying;
- never apply changes to approved segments automatically without new
  confirmation;
- keep both versions.

---

## 11. Frontend and UX

### 11.1 Main pages

| Page | Functions |
|---|---|
| Dashboard | projects, status, progress, jobs, available models |
| New project | PDF upload, metadata, genre, models, initial style guide |
| Import and structure | PDF viewer + extracted text + parts/chapters/scenes tree, boundary correction |
| Entities and glossary | entity table, aliases, evidence, genders, policies, CSV/TBX import/export |
| Translation | bilingual editor, batches, context, TM, termbase, QA, approval |
| QA | issue list, severity/chapter/type filters, side-by-side review |
| Prompts and models | versioned templates, parameters, model capability, token-budget preview |
| Export | XLIFF, TMX, DOCX, EPUB/HTML, CSV; selected/approved content only |
| Audit | jobs, LLM calls, snapshots, user actions, version diffs |

### 11.2 Bilingual editor

Three-panel layout:

1. **Left:** English source, synchronised PDF page, entity highlighting and
   OCR confidence.
2. **Centre:** editable Italian target, diff, Italian spellcheck, segment
   status.
3. **Right:** applicable glossary, TM match, entity evidence, QA issues,
   chapter notes, approve/reject/refine buttons.

Key functions:

- CAT-tool shortcuts (`Ctrl+Enter` approve, `Ctrl+M` TM, `Ctrl+G`
  glossary);
- search and replace with preview and scope (segment/chapter/project);
- filters "only unapproved", "only critical QA", "only OCR suspect";
- locking of approved segments;
- version history and side-by-side comparison;
- visible streaming for active jobs, but atomic DB writes after validation.

### 11.3 Progress dashboard

Show separately:

- parsed/OCR pages;
- confirmed chapters;
- proposed/verified/approved entities;
- translated words/segments;
- approved words/segments;
- QA issues by severity;
- model usage, tokens, timings and errors;
- TM reuse percentage.

---

## 12. Main APIs

### 12.1 Project and upload

```http
POST   /api/projects
GET    /api/projects/{projectId}
PATCH  /api/projects/{projectId}
POST   /api/projects/{projectId}/documents
POST   /api/projects/{projectId}/parse
GET    /api/projects/{projectId}/jobs
```

### 12.2 Structure

```http
GET    /api/projects/{projectId}/structure
PATCH  /api/projects/{projectId}/structure/nodes/{nodeId}
POST   /api/projects/{projectId}/structure/nodes
POST   /api/projects/{projectId}/structure/resegment
```

### 12.3 Entities and glossary

```http
POST   /api/projects/{projectId}/entities/extract
GET    /api/projects/{projectId}/entities
PATCH  /api/entities/{entityId}
POST   /api/entities/{entityId}/merge
GET    /api/entities/{entityId}/evidence
POST   /api/projects/{projectId}/glossary/import
GET    /api/projects/{projectId}/glossary/export
```

### 12.4 Translation and QA

```http
POST   /api/projects/{projectId}/translation/plan
POST   /api/projects/{projectId}/translation/run
POST   /api/translation-units/{unitId}/approve
POST   /api/translation-units/{unitId}/reject
POST   /api/translation-units/{unitId}/refine
POST   /api/projects/{projectId}/qa/run
GET    /api/projects/{projectId}/qa/issues
```

### 12.5 Gateway

```http
GET    /api/gateway/models
GET    /api/gateway/health
POST   /api/gateway/test-run
```

---

## 13. Security, privacy and copyright

### 13.1 Requirements

- All documents stay on encrypted local disk or encrypted local object
  storage.
- No submission to cloud endpoints; block and report non-local gateway
  endpoints when the project policy is `local_only`.
- Local OIDC/LDAP authentication or application accounts with RBAC.
- TLS even in the LAN for multi-user deployments.
- Encryption at rest for database and backups; keys managed outside the
  repository.
- Signed, temporary download URLs.
- Immutable audit log of accesses, exports, approvals and deletions.
- Configurable retention and "secure delete" for project assets.
- Optional watermark on draft exports.

### 13.2 Copyright

At upload the system must show a declaration: the user confirms owning or
administering the rights needed to process the text. The product must not
distribute uploaded texts nor use them for training, benchmarking or
telemetry without separate explicit opt-in.

---

## 14. Non-functional requirements

| Area | Requirement |
|---|---|
| Locality | 100% of data and inference on the local network by default |
| Reliability | Idempotent jobs; resume after restart; immutable snapshots |
| Import performance | 300-page digital PDF: initial parsing with progressive feedback; async OCR for scans |
| UI performance | chapter open < 2 s with local cache, except the initial PDF download |
| Scalability | job queue separate from the API; OCR/NLP/LLM workers scalable |
| Reproducibility | every LLM run keeps input hash, prompt/template, model, parameters and output hash |
| Accessibility | WCAG 2.2 AA for the main UI, keyboard shortcuts, contrast and screen readers |
| UI language | Italian; EN source and IT target; architecture ready for other pairs |
| Backup | versioned backup of DB + assets + prompts + glossary/TM snapshots |

---

## 15. Acceptance criteria

### 15.1 PDF and structure

- A PDF with a text layer produces per-page text with coordinates and hash.
- A scanned PDF activates OCR and shows per-page confidence.
- The system uses bookmarks when present and proposes chapters when absent.
- The user can fix a chapter boundary and regenerate only the dependent
  segments.
- Repeated headers/footers do not appear in the translated body after user
  confirmation.

### 15.2 Entities

- Isolated `I` with POS=pronoun never appears among the entities.
- Possessive `John's` produces a reversible normalisation; `John's late` and
  `Let's` are not altered.
- A character with aliases shows as a single entity after an approved merge.
- Every proposed entity shows at least one source mention/evidence.
- The user can set referential gender, Italian grammatical gender, number
  and translation policy.

### 15.3 Translation

- The frontend shows two distinct selectors, dynamically populated from the
  LLM Gateway.
- A block cannot start if the model lacks room for input + reserved output.
- Every LLM batch contains at most 16,384 tokens per the model's tokenizer.
- Every response keeps all and only the sent IDs.
- A `must_keep` entry is flagged if altered; a forbidden form is flagged.
- Context text never appears in the output.
- A translated output stays `machine_draft` until a user approves it.

### 15.4 QA and export

- Every QA issue can be linked to segment, span, category and severity.
- The system can export only approved segments, or include drafts with an
  explicit watermark.
- Every export keeps a manifest with project version, timestamp and segment
  count.
- Editing an approved term indicates which approved segments might now be
  inconsistent, without overwriting them.

---

## 16. Proposed roadmap

### Phase 0 — Technical discovery (2–3 weeks)

- Verify the real API and capabilities of the LLM Gateway.
- Test corpus: 3 native PDFs, 3 scans, 1 dialogue-heavy novel, 1 essay with
  notes.
- Local parser/OCR benchmark and BookNLP validation on real chapters.
- Tokenisation definition with the chosen gateway models.
- Prompt/JSON-schema prototype and terminology tests.

**Output:** technical ADRs, internal evaluation dataset, benchmark
baseline, final DB schema.

### Phase 1 — Import and structure MVP (4–6 weeks)

- Basic auth, projects, upload, object storage.
- Native PDF parsing + OCR fallback.
- PDF/text viewer and structure editor.
- Job queue, audit, status/progress.

**Definition of done:** the user uploads a PDF, fixes chapters and obtains
reliable segmented text.

### Phase 2 — Entities, glossary and models (4–6 weeks)

- BookNLP + LLM NER integration.
- Entity/alias/evidence CRUD; `I` and possessive handling.
- Termbase, CSV import/export, snapshots.
- Gateway adapter, capability matrix, two model selectors.

**Definition of done:** the user approves characters/terms before
translating.

### Phase 3 — Translation CAT (5–8 weeks)

- Bilingual segment editor.
- 16K planner, TM/glossary retrieval, prompt profiles.
- JSON-constrained output, validations, retry, branch runs.
- Translation memory and approvals.

**Definition of done:** a chapter can be translated locally, corrected and
approved with full traceability.

### Phase 4 — QA, export and hardening (4–6 weeks)

- Deterministic QA + QE/LLM critic.
- MQM annotations, reports, issue backlog.
- XLIFF/TMX/DOCX/HTML/EPUB exports.
- Backup, security, performance and end-to-end tests.

**Definition of done:** a complete project is exportable with glossary, TM,
audit and verifiable quality.

---

## 17. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Wrong OCR | High: translating the wrong text | per-line/per-page confidence, preview, OCR flags, re-OCR, source review |
| Bad chapter detection | High: wrong context and workflow | multi-signal detection + manual editor + versioned structure |
| LLM hallucination | High: additions/omissions | per-segment-ID output, critic, QA, human review, diff, no auto-approve |
| Inconsistent term | High on serial novels | approved termbase, constraints, validators, TM and recheck after edits |
| Gender bias | High for ambiguous characters | referential/grammatical distinction, mandatory evidence, `unknown`, review |
| Context overflow | Medium/high | real tokenizer, planner, output reserve, small blocks, retry with split |
| Gateway model outage | Medium | safe pause, idempotency, explicit fallback selection, branch runs |
| PDFs with anomalous layout | Medium | multi-parser pipeline, coordinates, OCR fallback and user correction |
| Copyright leakage | Critical | local-only network policy, audit, encryption, no training/telemetry by default |
| Misleading quality metric | Medium | use QE to prioritise review, never as automatic approval |

---

## 18. Success metrics

### 18.1 Technical metrics

- Percentage of pages parsed without intervention.
- OCR CER/WER on the annotated sample.
- Entity precision/recall and alias accuracy on the gold sample.
- Percentage of blocks passing JSON/ID validation on the first attempt.
- Terminology compliance rate.
- Average time per 1,000 source words, separating LLM wait and review.
- Rate of jobs resumed after failure.

### 18.2 Editorial metrics

- MQM errors per 1,000 words, by category and severity.
- Percentage of MT segments approved unchanged / lightly edited / rewritten.
- Coherence of approved forms over entities and terms.
- Average post-editing time per genre.
- Correlation between automatic QE and revisor assessment, calibrated per
  project.

Evaluation must be based on samples annotated by professional revisors: LLMs
do not automatically reach the quality of published human literary
translations, and aesthetic criteria remain hard to capture with automatic
metrics.

---

## 19. Open decisions

1. Actual gateway specification: endpoints, auth, formats, JSON-schema
   support, tokenisation, models and monitoring. *(Closed by
   [ADR-001](adr/ADR-001-gateway-capability-matrix.md).)*
2. Priority export format: editorial DOCX, CAT XLIFF or EPUB/HTML.
3. Licenses of the OCR/NLP components and commercial compatibility.
4. Policy for translator notes and editorial comments.
5. Per-project rules for italianising proper nouns, noble titles and
   toponyms.
6. Possible integration with a local Italian grammar checker.
7. Target hardware and desired concurrency: affects model size and the
   scheduler.
8. Contractual definition of "professional": review workflow, QA levels and
   approval signature.

---

## 20. Conclusion

The platform must be designed as a local editorial environment governed by
approved linguistic data, not as a simple chat interface that chops up and
translates a PDF. The components that determine quality are: extraction with
evidence and PDF recoverability; a user-confirmable structure; BookNLP +
coreference for the literary domain; versioned termbase and TM; blocks with
a real token budget; constrained and validated output; explainable QA; human
review and a complete audit trail.

This combination addresses the known weaknesses of LLMs in literary
translation — literalism, loss of variety and long-text incoherence — while
keeping the advantages of speed and contextual retrieval, without
sacrificing editorial control, privacy and reproducibility.
