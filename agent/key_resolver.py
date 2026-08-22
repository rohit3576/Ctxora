"""Key resolver: natural-language phrases -> verified canonical telemetry keys.

S5 of the pipeline. Matches alias phrases (longest first, word-boundary)
against the question, then keeps only keys that exist in the tenant's
registry — hallucinated keys never survive.
"""

import re
from dataclasses import dataclass

from knowledge.contracts import TenantKnowledge


@dataclass(frozen=True, slots=True)
class ResolvedKey:
    """One alias hit verified against the registry."""

    alias_matched: str
    canonical_key: str
    physical_key: str


@dataclass(frozen=True, slots=True)
class ResolvedKeys:
    """All verified keys for one question, in order of first appearance."""

    keys: tuple[ResolvedKey, ...]


class KeyResolver:
    """Resolve NL metric mentions to registered telemetry keys."""

    def resolve(self, question: str, knowledge: TenantKnowledge) -> ResolvedKeys:
        """Find alias hits in the question, verified against the registry."""
        registry = knowledge.keys_by_canonical()
        lookup = knowledge.alias_lookup()
        lowered = question.lower()

        phrases = sorted(lookup, key=len, reverse=True)
        hits: list[ResolvedKey] = []
        claimed_spans: list[tuple[int, int]] = []

        for phrase in phrases:
            for match in re.finditer(rf"\b{re.escape(phrase)}\b", lowered):
                span = match.span()
                if any(start < span[1] and span[0] < end for start, end in claimed_spans):
                    continue
                entry = lookup[phrase]
                registered = registry.get(entry.canonical_key)
                if registered is None:
                    continue
                hits.append(
                    ResolvedKey(
                        alias_matched=phrase,
                        canonical_key=entry.canonical_key,
                        physical_key=registered.physical_key,
                    )
                )
                claimed_spans.append(span)
                break

        by_canonical: dict[str, ResolvedKey] = {}
        for hit in hits:
            by_canonical.setdefault(hit.canonical_key, hit)
        return ResolvedKeys(keys=tuple(by_canonical.values()))
