"""LLM client contract: provider-pluggable, no vendor SDK outside this package."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GenResult:
    """One LLM completion with token accounting."""

    sql: str
    raw: str
    prompt_tokens: int
    completion_tokens: int


@runtime_checkable
class LLMClient(Protocol):
    """Any OpenAI-compatible endpoint satisfies this (Phase 1 impl)."""

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        """One completion with token accounting."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One embedding vector per input text."""
        ...
