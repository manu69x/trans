"""Pure entity-extraction layer (PRD §6.2, §6.3, §6.4).

Turns a plain-text stream (with page markers) into proposed entities with
mentions, evidence, referential gender and the §6.4 fields. No DB access and
no network: the runner (:mod:`backend.nlp_runner`) feeds it the chapter text
and BookNLP's raw output, and persists the results.

Pipeline (§6.2):
  1. BookNLP over the whole chapter/text — mentions, alias clustering
     (coref), referential-gender inference from pronouns, quote attribution.
  2. Additional transformer NER (local HF pipeline) for the categories
     BookNLP covers weakly: organisations, locations, works/media and
     artefacts (``misc`` bucket).
  3. Deterministic merge: identical normalised names unify into one entity;
     a mention inside a bigger mention maps to the same cluster (alias).
  4. §6.4 fields filled per category; every proposed entity carries at least
     one evidence with quote + page (§15.2).

Local-only (§13.1): models load from disk (``BOOKNLP_MODEL_PATH`` /
``HF_HOME`` cache); the pure layer itself never downloads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import preprocess as pre

# Page marker injected by :func:`build_flat_text` so char offsets map back to
# pages. Chosen to survive BookNLP's whitespace tokenisation (it filters
# whitespace tokens and collapses \n\n into paragraph breaks, but never
# removes a marker surrounded by newlines).
PAGE_MARK_PREFIX = "\n⟦PAGE:"
PAGE_MARK_SUFFIX = "⟧\n"
_PAGE_MARK_RE = re.compile(
    re.escape(PAGE_MARK_PREFIX) + r"(\d+)" + re.escape(PAGE_MARK_SUFFIX)
)


# ---------------------------------------------------------------------------
# §6.3 category mapping
# ---------------------------------------------------------------------------
# BookNLP entity categories (the part after the PROP/NOM/PRON prefix):
_BOOKNLP_CAT_TO_TYPE = {
    "PER": "PERSON",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
    "FAC": "LOCATION",
    "ORG": "ORG_FACTION",
    "VEH": "OBJECT_ARTIFACT",
}

# HF NER groups (word-level ``entity_group``) → §6.3 categories.
_HF_GROUP_TO_TYPE = {
    "PER": "PERSON",
    "ORG": "ORG_FACTION",
    "LOC": "LOCATION",
    "MISC": "WORK_MEDIA",  # disambiguated later by the kind heuristic
}


@dataclass
class Mention:
    """One surface occurrence of an entity (§6.2.6)."""

    text: str
    prop: str  # PROP | NOM | PRON
    page: int | None
    chapter_id: str | None
    char_start: int  # offset in the flat text fed to BookNLP
    char_end: int
    in_quote: bool = False


@dataclass
class ProposedEntity:
    """A candidate entity: one row of ``entities`` + its mentions/evidence."""

    canonical_source: str
    entity_type: str
    aliases: list[str] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)
    referential_gender: str = "unknown"
    referential_gender_evidence: str | None = None
    gender_confidence: float | None = None
    italian_grammatical_gender: str = "not_applicable"
    grammatical_number: str = "unknown"
    translation_policy: str = "undecided"
    confidence: float | None = None
    extractor: str = "booknlp"
    sources: list[str] = field(default_factory=list)

    @property
    def mention_count(self) -> int:
        return len(self.mentions)


@dataclass
class EntityExtraction:
    """Full result of one extraction run."""

    entities: list[ProposedEntity]
    text_sha256: str
    pages: list[int]
    categories_found: list[str]


# ---------------------------------------------------------------------------
# Text assembly
# ---------------------------------------------------------------------------
def build_flat_text(pages: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """Join page texts with page markers; returns ``(text, page_map)``.

    *pages* is ``[{"page_number": int, "text": str}, ...]`` in reading order
    (the caller strips confirmed headers/footers first, §5.3).  The returned
    ``page_map`` is a list of ``(char_start, char_end, page_number)`` spans:
    ``char_start``/``char_end`` delimit the page's own text *including* the
    marker that follows it, so every token offset of that page resolves to
    the right page number.
    """
    parts: list[str] = []
    page_map: list[tuple[int, int, int]] = []
    offset = 0
    for page in pages:
        text = (page.get("text") or "").strip()
        start = offset
        parts.append(text)
        offset += len(text)
        marker = PAGE_MARK_PREFIX + str(int(page["page_number"])) + PAGE_MARK_SUFFIX
        parts.append(marker)
        offset += len(marker)
        page_map.append((start, offset, int(page["page_number"])))
    return "".join(parts), page_map


def _page_for_offset(offset: int,
                     page_map: list[tuple[int, int, int]]) -> int | None:
    """The page whose span contains ``offset`` (linear scan; maps are short)."""
    for start, end, page in page_map:
        if start <= offset < end:
            return page
    return page_map[-1][2] if page_map and offset >= page_map[-1][1] else None


# ---------------------------------------------------------------------------
# BookNLP raw-output parsing (see docs/benchmarks/results-booknlp/extract.py)
# ---------------------------------------------------------------------------
def parse_booknlp_outputs(output_dir: str, file_id: str) -> dict:
    """Parse the standard BookNLP tab-separated outputs into dicts.

    Reads ``<file_id>.entities``, ``<file_id>.tokens`` and (optionally)
    ``<file_id>.book`` — the files BookNLP 1.0.7 writes (no ``.coref`` in
    this version: clustering lives in the ``COREF`` column of ``.entities``).
    """
    import json
    import os

    out: dict = {"entities": [], "tokens": [], "book": None}

    entities_path = os.path.join(output_dir, f"{file_id}.entities")
    if os.path.exists(entities_path):
        with open(entities_path, encoding="utf-8") as fh:
            for ln in fh.read().splitlines()[1:]:  # skip the header
                parts = ln.split("\t")
                if len(parts) < 6:
                    continue
                coref, start, end, prop, cat, text = parts[:6]
                out["entities"].append({
                    "coref": coref,
                    "start": int(start),
                    "end": int(end),
                    "prop": prop,
                    "cat": cat,
                    "text": text,
                })

    tokens_path = os.path.join(output_dir, f"{file_id}.tokens")
    if os.path.exists(tokens_path):
        with open(tokens_path, encoding="utf-8") as fh:
            for ln in fh.read().splitlines()[1:]:
                parts = ln.split("\t")
                if len(parts) < 8:
                    continue
                out["tokens"].append({
                    "paragraph_id": int(parts[0]),
                    "sentence_id": int(parts[1]),
                    "token_id": int(parts[3]),
                    "text": parts[4],
                    "byte_onset": int(parts[6]),
                    "byte_offset": int(parts[7]),
                })

    book_path = os.path.join(output_dir, f"{file_id}.book")
    if os.path.exists(book_path):
        with open(book_path, encoding="utf-8") as fh:
            out["book"] = json.load(fh)

    return out


# §15.2: "``I`` isolato e POS=pronome non compare tra le entità". BookNLP
# clusters first-person pronouns into their own group; those groups surface
# as PRON-only clusters whose whole surface is a first/second-person pronoun.
_FIRST_SECOND_PERSON = {"i", "me", "my", "myself", "we", "us", "our", "ours",
                        "you", "your", "yours", "yourself"}


def _is_pronoun_only_surface(text: str, prop: str) -> bool:
    """True for a PRON mention that is only a 1st/2nd-person pronoun."""
    if prop != "PRON":
        return False
    toks = [t for t in re.split(r"\s+", text.strip().lower()) if t]
    return bool(toks) and all(t in _FIRST_SECOND_PERSON for t in toks)


def _normalise_name(text: str) -> str:
    """Casefold + article/possessive strip for cross-source matching."""
    t = text.strip().lower()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"'s\s*$", "", t)
    t = re.sub(r"[^0-9a-zÀ-ɏ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def cluster_mentions(
    raw: dict,
    page_map: dict[int, int],
    chapter_id: str | None,
    total_pages: int,
) -> tuple[dict[str, ProposedEntity], list[dict]]:
    """Group BookNLP mentions into proposed entities per coref cluster.

    Returns ``(clusters, hf_only)`` — the second value is a placeholder list
    for mentions the transformer NER adds later (the runner merges them in).
    """
    tokens = raw["tokens"]
    clusters: dict[str, ProposedEntity] = {}
    drop_pronoun_cluster: set[str] = set()

    for ent in raw["entities"]:
        prop, cat = ent["prop"], ent["cat"]
        text = ent["text"]
        if cat == "O" or prop == "O":
            continue
        # §15.2: no isolated first/second-person pronouns as entities.
        if _is_pronoun_only_surface(text, prop):
            drop_pronoun_cluster.add(ent["coref"])
            continue

        entity_type = _BOOKNLP_CAT_TO_TYPE.get(cat)
        if entity_type is None:
            continue  # NUM / other non-§6.3 tags

        onset = tokens[ent["start"]]["byte_onset"] if ent["start"] < len(tokens) else 0
        endoff = tokens[ent["end"]]["byte_offset"] if ent["end"] < len(tokens) else onset

        coref = ent["coref"]
        cluster = clusters.get(coref)
        if cluster is None:
            cluster = clusters[coref] = ProposedEntity(
                canonical_source=text,
                entity_type=entity_type,
                extractor="booknlp",
            )
        else:
            if _normalise_name(text) != _normalise_name(cluster.canonical_source):
                cluster.aliases.append(text)

        quote = bool(re.search("[”“«»]", text))  # crude; refined by tokens
        cluster.mentions.append(Mention(
            text=text,
            prop=prop,
            page=_page_for_offset(onset, page_map),
            chapter_id=chapter_id,
            char_start=onset,
            char_end=endoff,
            in_quote=quote,
        ))

    # drop whole clusters that only ever appeared as 1st/2nd-person pronouns
    for coref in drop_pronoun_cluster:
        cluster = clusters.get(coref)
        if cluster is not None and all(
            m.prop == "PRON" for m in cluster.mentions
        ):
            del clusters[coref]

    # §5.5 hard rule (belt-and-braces with the PRON filter above): the
    # isolated pronoun ``I`` is never an entity, whatever surface any
    # residual mention/cluster carries.
    kept_clusters: dict[str, ProposedEntity] = {}
    for coref, cluster in clusters.items():
        if pre.is_isolated_I(cluster.canonical_source.strip()):
            continue
        kept_clusters[coref] = cluster
    clusters = kept_clusters

    return clusters, []


def _quote_spans(tokens: list[dict]) -> list[tuple[int, int]]:
    """Byte spans of double-quoted stretches from the token stream."""
    spans: list[tuple[int, int]] = []
    open_at: int | None = None
    for tok in tokens:
        text = tok["text"]
        if '"' in text or "“" in text or "”" in text:
            for ch in text:
                if ch in ('"', "“") and open_at is None:
                    open_at = tok["byte_onset"]
                elif ch in ('"', "”") and open_at is not None:
                    spans.append((open_at, tok["byte_offset"]))
                    open_at = None
    if open_at is not None:
        spans.append((open_at, tokens[-1]["byte_offset"] if tokens else 0))
    return spans


def attach_quote_flags(clusters: dict[str, ProposedEntity],
                       raw: dict) -> None:
    """Mark mentions that fall inside a quoted stretch (dialogue)."""
    spans = _quote_spans(raw["tokens"])
    for cluster in clusters.values():
        for m in cluster.mentions:
            m.in_quote = any(s <= m.char_start and m.char_end <= e
                             for s, e in spans)


def apply_gender(clusters: dict[str, ProposedEntity], raw: dict) -> None:
    """Copy BookNLP's pronoun-based gender inference into §6.4 fields.

    BookNLP's EM infers ``he/him/his`` / ``she/her`` / ``they/them/their`` /
    neopronoun distributions per character cluster (``.book`` → ``g``).
    The mapping is strictly referential (§6.4): it records which pronouns
    the text uses for the character — not their identity.  A low margin
    (< 0.05 between the top two) keeps ``unknown`` and records the tie.
    """
    book = raw.get("book") or {}
    per_cluster = {c["id"]: c for c in book.get("characters", [])}

    _PRON_TO_REF = {
        "he/him/his": "male",
        "she/her": "female",
        "they/them/their": "nonbinary",
    }

    for coref, cluster in clusters.items():
        chardata = per_cluster.get(int(coref)) if coref.lstrip("-").isdigit() else None
        g = (chardata or {}).get("g") or {}
        inference: dict[str, float] = g.get("inference") or {}
        if not inference:
            # fall back to the entity's own pronoun mentions
            pron_counts: dict[str, int] = {}
            for m in cluster.mentions:
                if m.prop == "PRON":
                    key = m.text.strip().lower()
                    pron_counts[key] = pron_counts.get(key, 0) + 1
            if pron_counts:
                best = max(pron_counts.items(), key=lambda kv: kv[1])
                cluster.referential_gender = _PRON_TO_REF.get(best[0], "unknown")
                cluster.gender_confidence = None
                cluster.referential_gender_evidence = (
                    f"pronoun mentions: {best[0]} x{best[1]}"
                )
            continue

        ranked = sorted(inference.items(), key=lambda kv: kv[1], reverse=True)
        top, top_val = ranked[0]
        margin = top_val - (ranked[1][1] if len(ranked) > 1 else 0.0)
        if margin < 0.05:
            cluster.referential_gender = "unknown"
            cluster.gender_confidence = round(top_val, 3)
            cluster.referential_gender_evidence = (
                f"ambiguous pronoun distribution: {top} {top_val:.3f} "
                f"vs {ranked[1][0]} {ranked[1][1]:.3f}"
                if len(ranked) > 1 else f"weak signal: {top} {top_val:.3f}"
            )
            continue

        cluster.referential_gender = _PRON_TO_REF.get(top, "unknown")
        cluster.gender_confidence = round(top_val, 3)
        cluster.referential_gender_evidence = (
            f"pronouns {top} (p={top_val:.3f})"
        )


def _apply_type_defaults(cluster: ProposedEntity, text: str) -> None:
    """Fill the §6.4 fields that follow deterministically from the category.

    Per §6.4: for non-personal entities the *Italian grammatical* gender —
    not the referential one — governs agreement, and it is NOT inferred from
    the English form; it stays ``not_applicable`` for human review.
    """
    if cluster.entity_type == "PERSON":
        # number: plural pronoun clusters ("they") stay for review
        if cluster.referential_gender == "nonbinary":
            cluster.referential_gender = cluster.referential_gender
        cluster.grammatical_number = "singular"
        cluster.translation_policy = "undecided"
        return
    if cluster.entity_type == "ROLE":
        cluster.grammatical_number = "singular"
        return
    # singularia/pluralia tantum need review; default unknown/singular
    if cluster.entity_type in ("CREATURE_SPECIES", "ORG_FACTION"):
        cluster.grammatical_number = "unknown"
    elif cluster.entity_type in ("OBJECT_ARTIFACT", "LOCATION", "WORK_MEDIA",
                                 "EVENT", "CONCEPT_TERM", "TITLE_HONORIFIC"):
        cluster.grammatical_number = "singular"
    cluster.italian_grammatical_gender = "not_applicable"
    cluster.translation_policy = "undecided"
    # referential gender is meaningless for non-personal entities
    cluster.referential_gender = "not_applicable"
    cluster.referential_gender_evidence = None
    cluster.gender_confidence = None


def apply_type_defaults(clusters: dict[str, ProposedEntity]) -> None:
    for cluster in clusters.values():
        _apply_type_defaults(cluster, cluster.canonical_source)


def compute_confidence(clusters: dict[str, ProposedEntity]) -> None:
    """Deterministic confidence: PROP weight 1.0, NOM 0.7, PRON 0.4.

    Averaged over mentions and damped towards the scale of evidence
    (more mentions → closer to the raw average, fewer → discounted).
    """
    weights = {"PROP": 1.0, "NOM": 0.7, "PRON": 0.4}
    for cluster in clusters.values():
        if not cluster.mentions:
            cluster.confidence = None
            continue
        vals = [weights.get(m.prop, 0.5) for m in cluster.mentions]
        raw = sum(vals) / len(vals)
        n = len(vals)
        damped = raw * (n / (n + 2.0)) + 0.3 * (2.0 / (n + 2.0))
        cluster.confidence = round(min(1.0, damped), 3)


def dedupe(clusters: dict[str, ProposedEntity]) -> dict[str, ProposedEntity]:
    """Merge clusters whose canonical name normalises the same.

    Keeps the bigger cluster (more mentions), absorbs the smaller one's
    mentions and aliases; aliases of the survivor are re-checked so the
    merged alias list has no duplicates (case-insensitive, normalised).
    """
    by_name: dict[str, str] = {}
    merged: dict[str, ProposedEntity] = {}
    for coref, cluster in clusters.items():
        key = _normalise_name(cluster.canonical_source)
        if not key:
            key = f"__raw_{coref}"
        existing_coref = by_name.get(key)
        if existing_coref is None:
            by_name[key] = coref
            merged[coref] = cluster
            continue
        keeper = merged[existing_coref]
        if len(cluster.mentions) > len(keeper.mentions):
            # the newcomer is the bigger one: swap contents
            merged[existing_coref] = cluster
            cluster.mentions.extend(keeper.mentions)
            cluster.aliases.extend(keeper.aliases)
            cluster.sources.extend(keeper.sources)
            # the previous keeper was already removed from `merged` when it
            # was itself demoted; pop() tolerates that (3+ same-name groups)
            merged.pop(coref, None)
            by_name[key] = existing_coref  # keep the map stable
        else:
            keeper.mentions.extend(cluster.mentions)
            keeper.aliases.extend(cluster.aliases)
            keeper.sources.extend(cluster.sources)
            # the loser may already have been swapped out of `merged`
            merged.pop(coref, None)

    for cluster in merged.values():
        canon = _normalise_name(cluster.canonical_source)
        seen = {canon}
        unique: list[str] = []
        for alias in cluster.aliases:
            if _normalise_name(alias) in seen:
                continue
            seen.add(_normalise_name(alias))
            unique.append(alias)
        cluster.aliases = unique
    return merged
