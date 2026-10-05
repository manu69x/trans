"""Markup helpers shared by the DOCX and EPUB/HTML exporters.

A :class:`~.collector.ExportSegment` target is free-form literary Italian.
The one inline convention that must survive into a typeset document is
*italics*: a title, a quoted phrase or a single word marked ``<i>...</i>`` /
``<em>...</em>`` (PRD §15.4 AC1 "corsivi preservati"). Em-dashes and smart
quotes are plain characters and pass through untouched.

:func:`render_inline` splits a target string into ``(is_italic, text)`` runs
so each exporter can apply the right style. Plain text (no markup) passes
through as a single non-italic run.
"""
from __future__ import annotations

import re

# <i ...>...</i> or <em ...>...</em>, non-greedy, dotall so multi-line
# em-dashed titles work.
ITALIC_RE = re.compile(r"<\s*(i|em)(\s[^>]*)?>.*?</\s*(?:i|em)\s*>",
                       re.DOTALL | re.IGNORECASE)


def _strip(text: str) -> str:
    """Remove any remaining HTML tags and unescape common entities."""
    text = re.sub(r"<[^>]+>", "", text)
    for entity, ch in (
        ("&nbsp;", "\u00a0"),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#39;", "'"),
        ("&apos;", "'"),
    ):
        text = text.replace(entity, ch)
    return text


def render_inline(text: str | None) -> list[tuple[bool, str]]:
    """Split *text* into ``(is_italic, raw_text)`` runs.

    Returns ``[(False, text)]`` for plain text, or a list of runs when the
    text contains ``<i>/<em>`` markup. Each run keeps its original (already
    tag-stripped) text so the exporter can apply the right style.
    """
    if text is None:
        return []
    if "<i" not in text.lower() and "<em" not in text.lower():
        stripped = _strip(text)
        return [(False, stripped)] if stripped else []

    runs: list[tuple[bool, str]] = []
    pos = 0
    for m in ITALIC_RE.finditer(text):
        start, end = m.start(), m.end()
        inner_start = text.find('>', m.start())
        if inner_start == -1:
            continue
        inner_end = text.rfind('<', m.start(), end)
        if inner_end == -1:
            continue
        if start > pos:
            head = _strip(text[pos:start])
            if head:
                runs.append((False, head))
        span = _strip(text[inner_start + 1:inner_end])
        if span:
            runs.append((True, span))
        pos = end
    if pos < len(text):
        tail = _strip(text[pos:])
        if tail:
            runs.append((False, tail))
    return runs
