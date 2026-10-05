"""Block planner: token budget, prompt assembly and conflict exposure.

Implements PRD §10.1 (per-block phases 1-3) and §9.3/§9.4:

* :class:`TokenBudget` -- real token accounting (step 1 of §10.1) using the
  selected model's tokenizer (tiktoken cl100k by default), with the §5.4
  initial split (10-11k source / 2-2.5k context / 2-3k headroom) and the
  hard 16,384-token block ceiling (§5.4 step 4).
* :class:`PromptAssembler` -- builds the translation prompt (§9.4) from
  **immutable snapshots** (glossary / TM / style) that are *referenced* by
  id, and applies the instruction hierarchy (§9.3).
* :class:`ConflictDetector` -- flags conflicting instructions between the
  glossary and the style guide (§9.3: "conflitti → esposti, non decisi in
  silenzio").

The planner never calls the LLM; it produces a serialisable plan that the
route turns into a Gateway call.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# --- token counting ---------------------------------------------------------
try:  # real tokenizer (PRD §10.1 step 1: "tokenizer reale del modello").
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")
    _encode = _ENCODING.encode

    def count_tokens(text: str) -> int:
        """Number of tokens in *text* via the real cl100k tokenizer."""
        if not text:
            return 0
        return len(_encode(text))

except Exception:  # pragma: no cover - tiktoken always present in this env
    import re

    _WORD_RE = re.compile(r"\S+")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_WORD_RE.findall(text))


# Tetto di blocco (rivisto 2026-09-21 su richiesta: input e output del
# modello entrambi a 16.384 token, allineato a llama-server -c 36864).
BLOCK_CEILING = 35264
# §5.4 recommended initial split.
DEFAULT_INITIAL_SOURCE = 16_384
DEFAULT_INITIAL_CONTEXT = 2_500
DEFAULT_HEADROOM = 16_384


@dataclass
class TokenBudget:
    """The token budget for one block (PRD §5.4 / §10.1 step 1).

    ``max_total`` is the hard ceiling (default 16,384, §5.4 step 4). The
    remaining headroom is what is left after source + context are counted
    against the ceiling; ``headroom`` is the recommended reserve for output
    and reasoning (§5.4): ``max_total - source - context``.
    """

    max_total: int = BLOCK_CEILING
    initial_source: int = DEFAULT_INITIAL_SOURCE
    initial_context: int = DEFAULT_INITIAL_CONTEXT
    headroom_target: int = DEFAULT_HEADROOM

    @property
    def context_budget(self) -> int:
        """Tokens available for context (glossary/TM/entities), §5.4."""
        return self.initial_context

    def remaining(self, source_tokens: int, context_tokens: int) -> int:
        """Headroom left for output after source + context, vs. the ceiling."""
        return self.max_total - source_tokens - context_tokens

    def within_ceiling(self, source_tokens: int, context_tokens: int) -> bool:
        return (source_tokens + context_tokens) <= self.max_total

    def summary(self, source_tokens: int, context_tokens: int) -> dict:
        return {
            "max_total": self.max_total,
            "source_tokens": source_tokens,
            "context_budget": self.context_budget,
            "context_tokens": context_tokens,
            "headroom": self.remaining(source_tokens, context_tokens),
            "headroom_target": self.headroom_target,
            "within_ceiling": self.within_ceiling(source_tokens, context_tokens),
            "total_used": source_tokens + context_tokens,
        }


# --- prompt assembly --------------------------------------------------------

# The fixed skeleton of the translation prompt (§9.4). Placeholders are
# substituted by :class:`PromptAssembler`; the surrounding text is the
# platform's hard rules and MUST NOT be altered per block.
_PROMPT_SKELETON = """RUOLO
Sei un traduttore editoriale professionista dall'inglese all'italiano.
Traduci con fedeltà semantica, naturalezza letteraria italiana e coerenza
assoluta con il glossario approvato. Non riassumere, non censurare, non
aggiungere spiegazioni, non omettere contenuto e non inventare dettagli.

PRIORITÀ
1. Conserva esattamente gli ID dei segmenti, i tag e i placeholder.
2. Applica le voci GLOSSARIO/ENTITÀ approvate. Le forme "non tradurre"
   devono rimanere identiche salvo flessione esplicitamente consentita.
3. "I isolato (PRD §5.5 / §9.4): il token isolato "I" è pronome personale
   di prima persona inglese, MAI un nome proprio.
4. Le marcature [[POSS:...]] 's indicano possessivo inglese. Rendilo in
   italiano in modo naturale; non emettere mai tali marcature.
5. Mantieni voce, tempo verbale, focalizzazione, ritmo dei dialoghi e
   punteggiatura significativa.
6. Il contesto è solo per comprensione: non tradurlo né restituirlo.

FILO GENERE
{genre_style_prompt}

GLOSSARIO ED ENTITÀ (solo voci approvate; adatta al contesto)
{retrieved_approved_terms}

MEMORIA DI TRADUZIONE (solo esempi approvati; adatta al contesto)
{tm_matches}

CONTESTO PRECEDENTE (non tradurre)
{previous_context}

TESTO DA TRADURRE
{segment_batch_with_ids}

OUTPUT
Restituisci esclusivamente JSON conforme allo schema. Un item per ogni ID
ricevuto. Schema:
{output_schema}
"""


def render_segment_batch(segments: list[dict]) -> str:
    """Render the ``{{segment_batch_with_ids}}`` section (PRD §9.4/§5.4).

    One line per segment, ``[<segment_id>] <source_text>`` — the model must
    echo the ids verbatim in the ``translations`` JSON (validated afterwards
    by :func:`backend.translation.validators.validate_ids`).
    """
    lines = []
    for s in segments:
        sid = str(s.get("segment_id") or "")
        text = s.get("source_text") or ""
        lines.append(f"[{sid}] {text}" if sid else text)
    return "\n".join(lines)


_GRAMMAR_GENDER_IT = {"masculine": "maschile", "feminine": "femminile"}
_GRAMMAR_NUMBER_IT = {"singular": "singolare", "plural": "plurale"}


def _grammar_it(entry: dict) -> str | None:
    """Genere/numero grammaticali italiani della voce (per l'articolo)."""
    gender = _GRAMMAR_GENDER_IT.get(
        (entry.get("gender")
         or entry.get("italian_grammatical_gender") or "").lower())
    number = _GRAMMAR_NUMBER_IT.get(
        (entry.get("number")
         or entry.get("grammatical_number") or "").lower())
    parts = [p for p in (gender, number) if p]
    return " ".join(parts) or None


def render_glossary_section(glossary_entries: list[dict],
                            entities: list[dict]) -> str:
    """Render ``{{retrieved_approved_terms}}`` (§9.4): the approved terms
    themselves, not a reference. Each line carries the policy (and the
    Italian grammatical gender/number, which drive article agreement) so the
    model can comply with §9.3/§10.2 without external lookups."""
    lines: list[str] = []
    for e in list(glossary_entries) + list(entities):
        src = (e.get("source") or e.get("canonical_source") or "").strip()
        if not src:
            continue
        tgt = (e.get("target") or e.get("canonical_target") or "").strip()
        policy = (e.get("policy") or "").lower()
        forbidden = [f.strip() for f in (e.get("forbidden_targets") or [])
                     if f and f.strip()]
        marks: list[str] = []
        if policy == "not_translate":
            body = f"{src} -> {tgt or src}"
            marks.append("NON TRADURRE: usa questa forma")
            # §9.4: «salvo flessione esplicitamente consentita» — il flag
            # dell'entità rende esplicita quella consenso nel prompt.
            if e.get("allow_inflection"):
                marks.append("flessione italiana consentita")
        elif tgt:
            body = f"{src} -> {tgt}"
        else:
            body = f"{src} (traduzione libera, coerente con il genere)"
        grammar = _grammar_it(e)
        if grammar:
            marks.append(grammar)
        if forbidden:
            marks.append("VIETATI: " + ", ".join(forbidden))
        if marks:
            body += "  [" + " · ".join(marks) + "]"
        lines.append(f"- {body}")
    return "\n".join(lines)


def render_tm_section(tm_matches: list[dict]) -> str:
    """Render ``{{tm_matches}}`` (§9.4): approved EN→IT example pairs."""
    lines: list[str] = []
    for m in tm_matches:
        src = (m.get("source_normalized") or m.get("source_original")
               or "").strip()
        tgt = (m.get("target_approved") or "").strip()
        if src and tgt:
            lines.append(f"- EN: {src}\n  IT: {tgt}")
    return "\n".join(lines)


def render_style_section(genre_profile: str | None,
                         style_guide: str | None) -> str:
    """Render ``{{genre_style_prompt}}`` (§9.3): the actual style guide when
    provided, always with the project's genre as the baseline."""
    parts: list[str] = []
    if style_guide and style_guide.strip():
        parts.append(style_guide.strip())
    if genre_profile and genre_profile.strip():
        parts.append(f"Genere dell'opera: {genre_profile.strip()}.")
    return "\n".join(parts)


@dataclass
class PromptAssembler:
    """Assembles the translation prompt (§9.4).

    The injected sections carry the *frozen snapshot contents* — the approved
    glossary/entity terms, the retrieved TM pairs and the style guide — so the
    prompt is self-contained: the model cannot resolve symbolic references,
    therefore the content itself must be in the prompt (fix 2026-09-18: the
    prompt rendered only ``[snapshot:<key>:<id>]`` markers, so glossary/TM
    constraints never reached the model). Snapshot ids remain on the
    :class:`BlockPlan` / run metadata for §16 reproducibility.
    """

    model_id: str
    glossary_entries: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    tm_matches: list[dict] = field(default_factory=list)
    genre_profile: str | None = None
    style_guide: str | None = None
    output_schema: str | None = None

    def render_section(self, key: str, value: str) -> str:
        return value if value not in (None, "") else "[[none]]"

    def assemble(self, *, previous_context: str = "",
                 segment_batch: str = "") -> str:
        """Render the full §9.4 prompt.

        *previous_context* feeds ``{{previous_context}}`` (context is for
        comprehension only, never translated back); *segment_batch* feeds
        ``{{segment_batch_with_ids}}`` — the rendered ``[id] source`` lines
        of the block (see :func:`render_segment_batch`).
        """
        return _PROMPT_SKELETON.format(
            genre_style_prompt=self.render_section(
                "style", render_style_section(self.genre_profile,
                                              self.style_guide)),
            retrieved_approved_terms=self.render_section(
                "glossary",
                render_glossary_section(self.glossary_entries, self.entities),
            ),
            tm_matches=self.render_section(
                "tm", render_tm_section(self.tm_matches)),
            previous_context=self.render_section("context", previous_context),
            segment_batch_with_ids=(
                segment_batch
                or render_segment_batch([{"segment_id": "", "source_text": ""}])
            ),
            output_schema=self.render_section("schema", self.output_schema or ""),
        )


def prompt_hash(prompt: str) -> str:
    """Deterministic hash of a prompt for reproducibility (§16)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# --- conflict detection -----------------------------------------------------

CONFLICT_LEVELS = ("none", "warn", "block")


@dataclass
class Conflict:
    kind: str
    term: str
    glossary_value: str
    style_value: str
    level: str
    message: str


class ConflictDetector:
    """§9.3: surface glossary/style-guide conflicts instead of hiding them.

    A conflict is when the style guide prescribes a form for a term that the
    approved glossary/entità says must be rendered differently (or must not be
    touched). The detector returns every such clash with a severity:

    * ``block`` -- the style guide would force a change that contradicts a
      ``must_keep``/``not_translate`` glossary entry (do not decide in
      silence; the reviewer must).
    * ``warn`` -- a softer disagreement (e.g. the style guide prefers a
      synonym the glossary did not mark forbidden).
    """

    def __init__(self, style_guide: str | None) -> None:
        self._guide = (style_guide or "").lower()

    def detect(
        self,
        terms: list[dict],
        *,
        block_source: str = "",
    ) -> list[Conflict]:
        """Return conflicts between the style guide and the given terms.

        Each *term* is the §7.3 payload (``source``/``target``/``forbidden``/
        ``policy``). A conflict is raised when the style guide mentions the
        term and prescribes a target that differs from the approved target or
        is itself a forbidden target of the glossary entry.
        """
        conflicts: list[Conflict] = []
        if not self._guide or not terms:
            return conflicts

        for t in terms:
            src = (t.get("source") or "").lower()
            if not src or src not in self._guide:
                continue
            target = (t.get("target") or "").lower()
            forbidden = [f.lower() for f in (t.get("forbidden_targets") or [])]
            policy = (t.get("policy") or "").lower()

            # the style guide names the term; is it telling us to use a
            # different target than the glossary approves?
            for prescribed in _find_in_text(self._guide, src):
                prescribed = prescribed.strip()
                if prescribed == src:
                    continue  # just mentions the term, no alternative form
                if prescribed in forbidden:
                    conflicts.append(
                        Conflict(
                            kind="style_vs_forbidden",
                            term=t.get("source") or src,
                            glossary_value=target or "<not translate>",
                            style_value=prescribed,
                            level="block",
                            message=(
                                "La style guide prescrive '{style}' ma il "
                                "glossario approvato lo vieta "
                                "(forbidden_targets). Richiede revisione umana."
                            ).format(style=prescribed),
                        )
                    )
                elif target and prescribed != target:
                    conflicts.append(
                        Conflict(
                            kind="style_vs_target",
                            term=t.get("source") or src,
                            glossary_value=target,
                            style_value=prescribed,
                            level="warn",
                            message=(
                                "La style guide prescrive '{style}, il "
                                "glossario approvato indica '{target}'."
                            ).format(style=prescribed, target=target),
                        )
                    )
        return conflicts


def _find_in_text(text: str, term: str):
    """Find occurrences of *term* in *text* that are followed by more text.

    Returns the matches where the term is immediately followed by other
    characters (i.e. a prescribed alternative form), or None.
    """
    import re

    pat = re.compile(re.escape(term) + r"[^\s]+")
    return pat.findall(text)


@dataclass
class BlockPlan:
    """The serialisable plan for one translation block.

    Carries everything the route needs to call Gateway and everything the UI
    / run metadata needs to show the human: the assembled prompt, the token
    budget, the retrieved TM matches, the injected glossary/entità and any
    exposed conflicts (§9.3).
    """

    model_id: str
    prompt: str
    prompt_hash: str
    budget: dict
    tm_matches: list[dict] = field(default_factory=list)
    glossary_entries: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    previous_context: str = ""
    conflicts: list[dict] = field(default_factory=list)
    snapshot_ids: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "prompt": self.prompt,
            "prompt_hash": self.prompt_hash,
            "budget": self.budget,
            "tm_matches": self.tm_matches,
            "glossary_entries": self.glossary_entries,
            "entities": self.entities,
            "previous_context": self.previous_context,
            "conflicts": self.conflicts,
            "snapshot_ids": self.snapshot_ids,
        }


def build_block_plan(
    *,
    model_id: str,
    block_source: str,
    segments: list[dict],
    previous_context: str = "",
    budget: TokenBudget | None = None,
    tm_matches: list[dict] | None = None,
    glossary_entries: list[dict] | None = None,
    entities: list[dict] | None = None,
    style_guide: str | None = None,
    snapshot_ids: dict | None = None,
    output_schema: str | None = None,
    genre_profile: str | None = None,
) -> BlockPlan:
    """Assemble a :class:`BlockPlan` for one block (PRD §10.1 steps 1-3).

    * counts source + context tokens with the real tokenizer (§10.1 step 1);
    * renders the §9.4 prompt from the snapshots (including the
      {{genre_style_prompt}} section from *genre_profile*);
    * detects glossary/style conflicts (§9.3 from the injected terms);
    * attaches the retrieved TM matches (§7.2).
    """
    budget = budget or TokenBudget()
    tm_matches = tm_matches or []
    glossary_entries = glossary_entries or []
    entities = entities or []
    snapshot_ids = snapshot_ids or {}

    source_tokens = count_tokens(block_source)
    # the token accounting MUST count exactly what the prompt embeds: the
    # rendered glossary/TM/style sections (fix 2026-09-18: the context was
    # counted from the real content but the prompt only carried markers).
    style_text = render_style_section(genre_profile, style_guide)
    glossary_text = render_glossary_section(glossary_entries, entities)
    tm_text = render_tm_section(tm_matches)
    context_text = "\n".join(x for x in (style_text, glossary_text, tm_text)
                             if x)
    context_tokens = count_tokens(context_text)
    budget_check = budget.remaining(source_tokens, context_tokens)

    assembler = PromptAssembler(
        model_id=model_id,
        glossary_entries=glossary_entries,
        entities=entities,
        tm_matches=tm_matches,
        genre_profile=genre_profile,
        style_guide=style_guide,
        output_schema=output_schema,
    )
    prompt = assembler.assemble(
        previous_context=previous_context,
        segment_batch=render_segment_batch(segments),
    )
    ph = prompt_hash(prompt)

    detector = ConflictDetector(style_guide)
    terms = [
        {
            "source": e.get("source"),
            "target": e.get("target"),
            "forbidden_targets": e.get("forbidden_targets") or [],
            "policy": e.get("policy"),
        }
        for e in glossary_entries
    ]
    conflicts = [c.__dict__ for c in detector.detect(terms, block_source=block_source)]

    return BlockPlan(
        model_id=model_id,
        prompt=prompt,
        prompt_hash=ph,
        budget=budget.summary(source_tokens, context_tokens),
        tm_matches=tm_matches,
        glossary_entries=glossary_entries,
        entities=entities,
        previous_context=previous_context,
        conflicts=conflicts,
        snapshot_ids=snapshot_ids,
    )


def _context_text(glossary_entries: list[dict], entities: list[dict], tm_matches: list[dict]) -> str:
    """Flat EN->IT rendering of the injected context (kept for callers/tests
    that need the plain form; the prompt embeds the richer sections and the
    budget counts those — see ``build_block_plan``)."""
    parts: list[str] = []
    for e in glossary_entries:
        src = e.get("source") or ""
        tgt = e.get("target") or ""
        if src or tgt:
            parts.append(f"{src} -> {tgt}")
    for e in entities:
        src = e.get("source") or ""
        tgt = e.get("target") or ""
        if src or tgt:
            parts.append(f"{src} -> {tgt}")
    for m in tm_matches:
        src = m.get("source_normalized") or m.get("source_original") or ""
        tgt = m.get("target_approved") or ""
        if src or tgt:
            parts.append(f"{src} -> {tgt}")
    return "\n".join(parts)
