"""Follow-up resolution: pronoun rewrite + metric carry-over from history."""

import re
from collections.abc import Sequence

from agent.conversation import ConversationContext, last_entity, last_metric

_PRONOUNS: re.Pattern[str] = re.compile(r"\b(it|its|that|this|them)\b", re.IGNORECASE)
_ENTITY_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:truck|device|sensor|meter|machine|vehicle)[-_]?\w*\d+\b", re.IGNORECASE
)


def resolve_followup(question: str, ctx: ConversationContext, aliases: Sequence[str]) -> str:
    """Rewrite pronoun shorthand into standalone questions.

    Rules: an entity pronoun swaps to the last-discussed entity; a follow-up
    carrying an entity but no metric gains the last-discussed metric.
    Questions already self-contained pass through untouched.
    """
    if not ctx.turns:
        return question

    has_entity = _ENTITY_PATTERN.search(question) is not None
    metric = last_metric(ctx, aliases)
    has_metric = metric is not None and _mentions(question, aliases)

    if _PRONOUNS.search(question) and not has_entity:
        entity = last_entity(ctx)
        if entity is not None:
            rewritten = _PRONOUNS.sub(entity, question, count=1)
            if metric is not None and not _mentions(rewritten, aliases):
                rewritten = f"{rewritten} (metric: {metric})"
            return rewritten

    if has_entity and metric is not None and not has_metric:
        return f"{question} (metric: {metric})"

    return question


def _mentions(question: str, aliases: Sequence[str]) -> bool:
    """Whether the question itself names a known metric alias."""
    lowered = question.lower()
    return any(re.search(rf"\b{re.escape(alias.lower())}\b", lowered) for alias in aliases)
