"""Answer summarization: rows -> short natural-language answer. S12."""

import json
from dataclasses import dataclass
from typing import Final

from database.contracts import JsonScalar
from llm.client import LLMClient

_MAX_ROWS_IN_PROMPT: Final = 20

_SYSTEM: Final = (
    "You answer questions about telemetry query results. Use the question, "
    "the SQL, and the rows. Answer in 1-3 short sentences with concrete "
    "numbers and units from the rows. Never invent data."
)


@dataclass(frozen=True, slots=True)
class Summary:
    """One natural-language answer."""

    text: str


class Summarizer:
    """Turn query rows into a short answer."""

    def __init__(self, llm: LLMClient) -> None:
        """Bind the LLM client used for every summary call."""
        self.llm: LLMClient = llm

    def summarize(
        self,
        question: str,
        rows: list[dict[str, JsonScalar]],
        sql: str,
    ) -> Summary:
        """Summarize the rows as an answer to the question."""
        if not rows:
            return Summary(text="The query returned no rows for the given filters.")
        payload = json.dumps(rows[:_MAX_ROWS_IN_PROMPT], default=str)
        user = f"QUESTION: {question}\nSQL: {sql}\nROWS: {payload}"
        result = self.llm.generate(_SYSTEM, user, temperature=0.0)
        return Summary(text=result.raw.strip())
