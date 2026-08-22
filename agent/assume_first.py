"""Assume-first: fill missing dimensions with defaults, state the assumptions."""

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Final

_TIME_WORDS: Final = re.compile(
    r"\b(today|yesterday|last|this|past|week|day|hour|month|minute|now)\b", re.IGNORECASE
)
_AGG_WORDS: Final = re.compile(
    r"\b(average|avg|maximum|max|minimum|min|latest|last|sum|total|trend)\b", re.IGNORECASE
)
_ENTITY_PATTERN: Final = re.compile(
    r"\b(?:truck|device|sensor|meter|machine|vehicle)[-_]?\w*\d+\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Assumptions:
    """Augmented question plus the human-readable assumption note."""

    question: str
    note: str | None


def default_aggregation(
    key: str, key_aggregations: dict[str, str], defaults: dict[str, str]
) -> str:
    """Most specific aggregation default for one key (globs before star)."""
    registered = key_aggregations.get(key)
    if registered:
        return registered
    for pattern, aggregation in sorted(defaults.items(), key=lambda kv: kv[0] != "*"):
        if pattern == "*":
            return aggregation
        if fnmatch(key, pattern):
            return aggregation
    return "average"


def assume_first(
    question: str,
    resolved_keys: tuple[str, ...],
    key_aggregations: dict[str, str],
    defaults: dict[str, str],
    default_window: str,
) -> Assumptions:
    """Append explicit qualifiers for missing dimensions and explain them."""
    parts: list[str] = []
    notes: list[str] = []

    if not _AGG_WORDS.search(question) and resolved_keys:
        key = resolved_keys[0]
        aggregation = default_aggregation(key, key_aggregations, defaults)
        parts.append(f"use {aggregation}")
        notes.append(f"aggregation {aggregation} (default for '{key}')")

    if not _TIME_WORDS.search(question):
        parts.append(f"time window {default_window}")
        notes.append(f"time window {default_window}")

    if not _ENTITY_PATTERN.search(question):
        parts.append("across all devices")
        notes.append("fleet-wide scope")

    if not parts:
        return Assumptions(question=question, note=None)
    augmented = f"{question} [{'; '.join(parts)}]"
    return Assumptions(question=augmented, note="assuming " + ", ".join(notes))
