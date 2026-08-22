"""Deterministic session titles from config keywords (no LLM call)."""

import re
from collections.abc import Sequence

_FALLBACK = "Telemetry Query"


def title_for(question: str, keywords: Sequence[str]) -> str:
    """First keyword (config order) present as a whole word in the question."""
    lowered = question.lower()
    for keyword in keywords:
        if re.search(rf"\b{re.escape(keyword.lower())}\b", lowered):
            return f"{keyword.title()} Query"
    return _FALLBACK
