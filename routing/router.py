"""Intent routing: data vs docs vs hybrid, from config indicators."""

import re
from dataclasses import dataclass
from typing import Literal

from config.settings import RoutingConfig

Intent = Literal["data", "docs", "hybrid"]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """One classified question."""

    intent: Intent
    data_indicators: tuple[str, ...]
    docs_indicators: tuple[str, ...]


def _hits(question: str, indicators: tuple[str, ...]) -> tuple[str, ...]:
    """Whole-word indicator phrases present in the question."""
    lowered = question.lower()
    return tuple(
        indicator for indicator in indicators if re.search(rf"\b{re.escape(indicator)}\b", lowered)
    )


def classify(question: str, routing: RoutingConfig) -> RouteDecision:
    """Classify by configured indicator lists; both lists hit -> hybrid."""
    data_hits = _hits(question, routing.sql_indicators)
    docs_hits = _hits(question, routing.rag_indicators)
    if data_hits and docs_hits:
        return RouteDecision("hybrid", data_hits, docs_hits)
    if data_hits:
        return RouteDecision("data", data_hits, ())
    if docs_hits:
        return RouteDecision("docs", (), docs_hits)
    return RouteDecision("data", (), ())
