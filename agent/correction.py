"""Correction detection: triggers, guards, LLM classifier, one-regen cap.

Outcomes (frozen, exhaustive union):
- NotCorrection: treat the message as a fresh question
- Correction: regenerate `corrected_question` (temperature 0.3 upstream)
- Clarify: the one-regeneration cap fired; ask a targeted question instead
"""

import re
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from llm.client import LLMClient
from memory.contracts import HistoryTurn

_TRIGGERS: tuple[str, ...] = (
    "no",
    "wrong",
    "incorrect",
    "i meant",
    "instead of",
    "try again",
    "not what",
)
_GUARDS: tuple[str, ...] = ("no data", "no results", "nothing", "now", "know")

_SYSTEM: str = (
    "You classify messages in a telemetry Q&A conversation. The previous answer may be "
    "wrong and the user is replying. Reply with ONLY a JSON object: "
    '{"is_correction": true|false, "corrected_question": "the full corrected question"} '
    "where corrected_question merges the user's intent with the original question. "
    "Messages that merely report missing data or ask for new information are NOT corrections."
)

_CLARIFY_QUESTION: str = (
    "I want to get this right — could you tell me exactly what you'd like to see? "
    "For example: which entity (e.g. truck-102), which measurement "
    "(average, maximum, or latest), and for which day?"
)


class _Classification(BaseModel):
    """Parsed classifier verdict."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")

    is_correction: bool
    corrected_question: str = ""


@dataclass(frozen=True, slots=True)
class NotCorrection:
    """The message is a fresh question, not a complaint."""


@dataclass(frozen=True, slots=True)
class Correction:
    """The user is correcting the previous answer."""

    corrected_question: str


@dataclass(frozen=True, slots=True)
class Clarify:
    """One-regeneration cap fired: ask instead of guessing again."""

    question: str = _CLARIFY_QUESTION


CorrectionOutcome = NotCorrection | Correction | Clarify


def _has_word(question: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", question.lower()) is not None


class CorrectionDetector:
    """Decide whether a message corrects the previous turn."""

    def __init__(self, llm: LLMClient) -> None:
        """Bind the LLM used only for guard-demoted classification."""
        self._llm: LLMClient = llm
        self.llm_calls: int = 0

    def detect(self, message: str, previous: HistoryTurn | None) -> CorrectionOutcome:
        """Classify one follow-up message against the previous turn."""
        if previous is None:
            return NotCorrection()
        if previous.supersedes_id is not None and self._hits_trigger(message):
            return Clarify()

        if not self._hits_trigger(message):
            return NotCorrection()

        if not any(_has_word(message, guard) for guard in _GUARDS):
            return Correction(corrected_question=self._merge(message, previous))

        return self._classify(message, previous)

    def _hits_trigger(self, message: str) -> bool:
        return any(_has_word(message, trigger) for trigger in _TRIGGERS)

    def _merge(self, message: str, previous: HistoryTurn) -> str:
        """Fast path: pair the complaint with the original question."""
        return f"{previous.nl_query} (user corrects: {message})"

    def _classify(self, message: str, previous: HistoryTurn) -> CorrectionOutcome:
        """LLM fallback for guard-demoted messages; fails open."""
        self.llm_calls += 1
        user = (
            f"ORIGINAL QUESTION: {previous.nl_query}\n"
            f"PREVIOUS ANSWER: {previous.summary}\n"
            f"USER MESSAGE: {message}"
        )
        result = self._llm.generate(_SYSTEM, user, temperature=0.0)
        try:
            verdict = _Classification.model_validate_json(result.raw)
        except ValidationError:
            return NotCorrection()
        if not verdict.is_correction or not verdict.corrected_question:
            return NotCorrection()
        return Correction(corrected_question=verdict.corrected_question)
