"""SQL generation: LLM call + fenced-block extraction. S8 of the pipeline."""

import re
from dataclasses import dataclass

from llm.client import GenResult, LLMClient

_FENCED: re.Pattern[str] = re.compile(r"```sql\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


class GenerationError(Exception):
    """The LLM response contained no usable SQL statement."""

    def __init__(self, detail: str) -> None:
        """Describe why extraction failed."""
        self.detail: str = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """Generated SQL with token accounting."""

    sql: str
    prompt_tokens: int
    completion_tokens: int


class SQLGenerator:
    """Generate one SQL statement from assembled prompts."""

    def __init__(self, llm: LLMClient) -> None:
        """Bind the LLM client used for every generation call."""
        self.llm: LLMClient = llm

    def generate(self, system: str, user: str, *, temperature: float = 0.0) -> GenerationOutcome:
        """Call the LLM and extract the fenced SQL."""
        result: GenResult = self.llm.generate(system, user, temperature=temperature)
        matches = _FENCED.findall(result.raw)
        if not matches:
            msg = "response contained no ```sql fenced block"
            raise GenerationError(msg)
        sql = matches[-1].strip()
        if not sql:
            msg = "fenced sql block was empty"
            raise GenerationError(msg)
        return GenerationOutcome(
            sql=sql,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
