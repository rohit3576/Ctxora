"""Conversation context: prior turns + entity/metric extraction."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from memory.contracts import HistoryTurn

_ENTITY_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:truck|device|sensor|meter|machine|vehicle)[-_]?\w*\d+\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """Prior turns of the active session, oldest first."""

    turns: tuple[HistoryTurn, ...]

    @property
    def latest(self) -> HistoryTurn | None:
        """Most recent turn."""
        return self.turns[-1] if self.turns else None


def last_entity(ctx: ConversationContext) -> str | None:
    """Most recent device-like token across the conversation."""
    for turn in reversed(ctx.turns):
        match = _ENTITY_PATTERN.search(turn.nl_query)
        if match:
            return match.group(0).lower()
    return None


def last_metric(ctx: ConversationContext, aliases: Sequence[str]) -> str | None:
    """Most recent alias phrase present in a prior question."""
    for turn in reversed(ctx.turns):
        lowered = turn.nl_query.lower()
        for alias in sorted(aliases, key=len, reverse=True):
            if re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
                return alias
    return None
