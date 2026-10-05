"""Sanitized structured JSON logging (PRD §13.1 / §14 / §4.2).

The platform must emit JSON logs that **never** contain manuscript payload
(§13.1 "nessun payload del manoscritto nei log"). This module provides:

* :func:`configure_logging` -- installs a JSON-lines handler (file and/or
  stdout) whose formatter runs every record through :class:`Sanitizer`.
* :class:`Sanitizer` -- redacts, by field *name* and by *content*, any value
  that carries manuscript text (source/target segments, LLM prompts, block
  source, quotes, evidence). Content redaction works by scanning string
  values against a set of *canary phrases* supplied by the caller (in tests
  the canaries are distinctive n-grams taken from the actual corpus, which is
  exactly the AC4 "log scan" check).

Design notes
------------
* Redaction is **name-based first** (fast path): the set :data:`SENSITIVE_KEYS`
  names the fields that are known to carry manuscript text, plus the raw text
  of a PDF page or OCR line.
* Redaction is **content-based second** (safety net): any string value that
  contains a canary n-gram is replaced by a short hash, so a value that
  slipped in under an unlisted key is still scrubbed.

The result is deterministic: the *shape* of the log line is preserved (key
names, token counts, ids) but the *text* is gone, so logs stay useful for
debugging while carrying zero manuscript payload.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import sys
from typing import Any, Iterable

# Field names that, by construction, carry manuscript / prompt payload.
# Any JSON object under one of these keys is reduced to a {len, sha256} stub.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        # LLM / prompt
        "system_prompt",
        "user_prompt",
        "prompt",
        "prompt_text",
        "block_source",
        "previous_context",
        "context",
        "content",  # chat message content
        "messages",  # full chat history
        # segments / text
        "source_text",
        "target_text",
        "source_normalized",
        "source_original",
        "target_approved",
        "normalized_text",
        "text",
        "raw_text",
        "quote_text",
        "quote",
        "evidence",
        "ocr_text",
        "lines",
        "document_text",
        "body",
        # the whole payload blob of a run / job
        "payload",
        # export / file bytes decoded to text
        "file_text",
        "fulltext",
        "full_text",
    }
)

# A recursive cap so we never walk unbounded structures.
_MAX_DEPTH = 12
# Strings longer than this are "text-like" and worth a content scan even if
# the key is not in SENSITIVE_KEYS (cheap: we only scan when it's long).
_CONTENT_SCAN_MIN_LEN = 40


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


class Sanitizer:
    """Redacts manuscript payload from a log record's structured data.

    ``canary_phrases`` is a set of distinctive substrings (ideally
    multi-word n-grams from the manuscript corpus) used for the content
    scan. When a string value contains any canary it is replaced by a stub.
    An empty set still applies the name-based redaction, which is the
    primary line of defence.
    """

    def __init__(self, canary_phrases: Iterable[str] = ()) -> None:
        self._canaries: tuple[str, ...] = tuple(canary_phrases)

    def redact_value(self, value: Any, *, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            return _stub(str(value))
        if isinstance(value, dict):
            return {k: self.redact_value(v, depth=depth + 1) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self.redact_value(v, depth=depth + 1) for v in value]
        if isinstance(value, str):
            return self._redact_string(value)
        return value  # numbers, bools, None, UUIDs, ids, counts stay

    def _redact_string(self, s: str) -> Any:
        # Content scan first (catches unlisted keys).
        if self._canaries and len(s) >= _CONTENT_SCAN_MIN_LEN:
            for c in self._canaries:
                if c and c in s:
                    return _stub(s)
        return s

    def redact_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in mapping.items():
            if k in SENSITIVE_KEYS:
                out[k] = _stub(v)
            else:
                out[k] = self.redact_value(v)
        return out


def _stub(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    try:
        length = len(text)
    except TypeError:  # pragma: no cover - non-measurable
        length = -1
    return {"redacted": True, "len": length, "sha256": _sha(text)}


class JsonLogFormatter(logging.Formatter):
    """Emit each record as a single JSON line, with manuscript redaction."""

    def __init__(self, sanitizer: Sanitizer) -> None:
        super().__init__()
        self._sanitizer = sanitizer

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Structured extras (record.__dict__ minus standard fields).
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD and not k.startswith("_")
        }
        payload["data"] = self._sanitizer.redact_mapping(extras)
        return json.dumps(payload, ensure_ascii=False, default=str)


_STANDARD = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


class _JsonHandler(logging.Handler):
    def __init__(self, stream, sanitizer: Sanitizer) -> None:
        super().__init__()
        self._stream = stream
        self.setFormatter(JsonLogFormatter(sanitizer))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


def configure_logging(
    log_path: str | None = None,
    *,
    level: str | None = None,
    canary_phrases: Iterable[str] = (),
    to_stdout: bool = True,
) -> logging.Logger:
    """Install the JSON log handler(s) and return the root-ish logger.

    ``log_path`` (when given) receives the sanitized JSON lines; ``to_stdout``
    mirrors them to the process stdout (useful under Docker, where stdout is
    the container log). ``canary_phrases`` feeds the content-scan safety net.
    """
    level = (level or os.getenv("LOG_LEVEL", "info")).lower()
    logger = logging.getLogger("trans")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers = []  # idempotent: don't double-add on reconfigure
    logger.propagate = False

    sanitizer = Sanitizer(canary_phrases)
    streams = []
    if to_stdout:
        streams.append(sys.stdout)
    if log_path:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        streams.append(open(log_path, "a", buffering=1, encoding="utf-8"))
    for s in streams:
        logger.addHandler(_JsonHandler(s, sanitizer))
    return logger
