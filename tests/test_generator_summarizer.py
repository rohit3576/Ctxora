"""Generator + summarizer tests: fenced-SQL extraction, LLM error mapping."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

import pytest

from agent.generator import GenerationError, SQLGenerator
from agent.summarizer import Summarizer

if TYPE_CHECKING:
    from database.contracts import JsonScalar
from llm.client import GenResult, LLMClient
from tests.fakes import FakeLLM


class ScriptedLLM:
    """Standalone fake: always returns one scripted raw response."""

    def __init__(self, raw: str) -> None:
        self.raw: str = raw

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(sql="", raw=self.raw, prompt_tokens=2, completion_tokens=3)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class CapturingLLM:
    """Standalone fake that records prompts and returns one scripted response."""

    def __init__(self, raw: str) -> None:
        self.raw: str = raw
        self.seen_user: str = ""
        self.seen_temperature: float = -1.0

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        self.seen_user = user
        self.seen_temperature = temperature
        return GenResult(sql="", raw=self.raw, prompt_tokens=2, completion_tokens=3)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


class TestSQLGenerator:
    def test_extracts_fenced_sql_block(self) -> None:
        raw = "Sure!\n```sql\nSELECT 1\n```\nHope that helps."
        generator = SQLGenerator(llm=ScriptedLLM(raw))

        result = generator.generate(system="s", user="u")

        assert result.sql == "SELECT 1"
        assert result.prompt_tokens == 2

    def test_temperature_zero_is_used(self) -> None:
        llm = CapturingLLM("```sql\nSELECT 1\n```")
        SQLGenerator(llm=llm).generate("s", "u")

        assert llm.seen_temperature == 0.0

    def test_missing_fence_raises_generation_error(self) -> None:
        generator = SQLGenerator(llm=ScriptedLLM("SELECT 1 without fence"))

        with pytest.raises(GenerationError, match="fenced"):
            generator.generate("s", "u")

    def test_empty_block_raises_generation_error(self) -> None:
        generator = SQLGenerator(llm=ScriptedLLM("```sql\n\n```"))

        with pytest.raises(GenerationError):
            generator.generate("s", "u")


class TestSummarizer:
    def test_summarizes_rows_with_question(self) -> None:
        llm = CapturingLLM("Avg was 61.4 rpm.")
        summarizer = Summarizer(llm=llm)
        rows: list[dict[str, JsonScalar]] = [{"device_id": "truck-102", "avg_rpm": 1487.5}]

        summary = summarizer.summarize(
            question="average rpm of truck-102 yesterday?",
            rows=rows,
            sql="SELECT ...",
        )

        assert summary.text == "Avg was 61.4 rpm."
        assert "truck-102" in llm.seen_user
        assert "1487.5" in llm.seen_user

    def test_empty_rows_get_honest_summary(self) -> None:
        summarizer = Summarizer(llm=FakeLLM())

        summary = summarizer.summarize(question="any data?", rows=[], sql="SELECT ...")

        assert "no rows" in summary.text.lower()


class TestProtocolConformance:
    def test_fakes_satisfy_llm_contract(self) -> None:
        assert isinstance(ScriptedLLM("x"), LLMClient)
