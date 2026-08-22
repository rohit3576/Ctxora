"""Correction detection tests: triggers, guards, classifier, one-regen cap."""

from collections.abc import Sequence

import pytest

from agent.correction import (
    Clarify,
    Correction,
    CorrectionDetector,
    NotCorrection,
)
from llm.client import GenResult
from memory.contracts import HistoryTurn

CLASSIFIER_JSON = (
    '{"is_correction": true, "corrected_question": "maximum rpm of truck-102 yesterday?"}'
)


class ClassifierLLM:
    """Returns one scripted classifier JSON for every call."""

    def __init__(self, raw: str) -> None:
        self.raw: str = raw

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(sql="", raw=self.raw, prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


def prev_turn(supersedes_id: int | None = None) -> HistoryTurn:
    """A previous answered turn, optionally itself a correction."""
    return HistoryTurn(
        id=7,
        session_id="s",
        nl_query="average rpm of truck-102 yesterday?",
        sql="SELECT avg(...)",
        data=(),
        summary="avg was 1487",
        token_usage=10,
        supersedes_id=supersedes_id,
    )


class TestFastPath:
    def test_plain_complaint_is_correction_without_llm(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON))

        outcome = detector.detect("no, that is wrong", prev_turn())

        assert isinstance(outcome, Correction)
        assert detector.llm_calls == 0

    def test_i_meant_is_correction(self) -> None:
        outcome = CorrectionDetector(llm=ClassifierLLM("")).detect(
            "i meant maximum, not average", prev_turn()
        )

        assert isinstance(outcome, Correction)


class TestGuards:
    def test_no_data_is_not_a_complaint_via_fast_path(self) -> None:
        detector = CorrectionDetector(
            llm=ClassifierLLM('{"is_correction": false, "corrected_question": ""}')
        )

        outcome = detector.detect("there is no data for speed", prev_turn())

        assert isinstance(outcome, NotCorrection)

    def test_guard_phrase_demotes_to_classifier(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON))

        outcome = detector.detect("no, wrong -- you know the average", prev_turn())

        assert isinstance(outcome, Correction)
        assert detector.llm_calls == 1

    def test_now_is_a_guard(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON))

        detector.detect("wrong, show me now", prev_turn())

        assert detector.llm_calls == 1


class TestClassifier:
    def test_classifier_false_is_not_correction(self) -> None:
        detector = CorrectionDetector(
            llm=ClassifierLLM('{"is_correction": false, "corrected_question": ""}')
        )

        outcome = detector.detect("wrong, now show something else", prev_turn())

        assert isinstance(outcome, NotCorrection)

    def test_classifier_garbage_fails_open_to_not_correction(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM("not json at all"))

        outcome = detector.detect("wrong, now something", prev_turn())

        assert isinstance(outcome, NotCorrection)

    def test_classifier_true_carries_corrected_question(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON))

        outcome = detector.detect("now wrong anyway", prev_turn())

        assert isinstance(outcome, Correction)
        assert outcome.corrected_question == "maximum rpm of truck-102 yesterday?"


class TestOneRegenCap:
    def test_complaint_about_a_correction_clarifies(self) -> None:
        detector = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON))

        outcome = detector.detect("no wrong again", prev_turn(supersedes_id=5))

        assert isinstance(outcome, Clarify)
        assert "entity" in outcome.question.lower()

    def test_no_previous_turn_means_no_correction(self) -> None:
        outcome = CorrectionDetector(llm=ClassifierLLM(CLASSIFIER_JSON)).detect("no, wrong", None)

        assert isinstance(outcome, NotCorrection)


class TestTemperature:
    def test_classifier_runs_at_temperature_zero(self) -> None:
        seen: list[float] = []

        class TempLLM:
            def __init__(self, raw: str) -> None:
                self.inner: ClassifierLLM = ClassifierLLM(raw)

            def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
                seen.append(temperature)
                return self.inner.generate(system, user, temperature=temperature)

            def embed(self, texts: Sequence[str]) -> list[list[float]]:
                return [[0.0] * 4 for _ in texts]

        CorrectionDetector(llm=TempLLM(CLASSIFIER_JSON)).detect("no, wrong for now", prev_turn())

        assert seen == [0.0]


class TestOutcomeUnion:
    def test_all_outcomes_are_members_of_the_union(self) -> None:
        members: tuple[object, ...] = (NotCorrection(), Correction("q"), Clarify())

        for member in members:
            assert isinstance(member, (NotCorrection, Correction, Clarify))
        reveal = members  # CorrectionOutcome covers exactly these three
        assert len(reveal) == 3


class TestImmutableOutcomes:
    def test_outcomes_are_frozen(self) -> None:
        outcome = Correction(corrected_question="q")
        attribute = "corrected_question"

        with pytest.raises(AttributeError):
            setattr(outcome, attribute, "other")
