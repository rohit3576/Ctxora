"""Session digest: rolling LLM summary of long conversations."""

from dataclasses import dataclass

from llm.client import LLMClient
from memory.contracts import HistoryTurn

_SYSTEM: str = (
    "Summarize this telemetry Q&A conversation in at most five short bullet "
    "lines: each entity, metric, and conclusion already established. "
    "Reply with the bullets only."
)
_MAX_TURNS_IN_PROMPT: int = 20


@dataclass(frozen=True, slots=True)
class Digest:
    """One session's rolling summary."""

    text: str


class DigestCache:
    """Per-session digests, rebuilt only when the turn count grows."""

    def __init__(self, llm: LLMClient, turn_threshold: int) -> None:
        """Bind the LLM and the minimum turn count that triggers digests."""
        self._llm: LLMClient = llm
        self._threshold: int = turn_threshold
        self._cache: dict[str, tuple[int, str]] = {}

    def digest_for(self, session_id: str, turns: tuple[HistoryTurn, ...]) -> Digest | None:
        """Return the cached digest, rebuild when stale, None below threshold."""
        if len(turns) < self._threshold:
            return None
        cached = self._cache.get(session_id)
        if cached is not None and cached[0] == len(turns):
            return Digest(text=cached[1])
        text = self._build(turns)
        self._cache[session_id] = (len(turns), text)
        return Digest(text=text)

    def _build(self, turns: tuple[HistoryTurn, ...]) -> str:
        """Ask the LLM for the rolling summary."""
        lines = [
            f"Q: {turn.nl_query} -> A: {turn.summary}" for turn in turns[-_MAX_TURNS_IN_PROMPT:]
        ]
        result = self._llm.generate(_SYSTEM, "\n".join(lines), temperature=0.0)
        return result.raw.strip()
