"""Follow-up resolution + assume-first tests."""

from agent.assume_first import Assumptions, assume_first
from agent.conversation import ConversationContext
from agent.followup import resolve_followup
from memory.contracts import HistoryTurn


def turn(q: str) -> HistoryTurn:
    """One minimal history turn."""
    return HistoryTurn(
        id=1, session_id="s", nl_query=q, sql="SELECT 1", data=(), summary="", token_usage=0
    )


class TestResolveFollowup:
    def test_pronoun_replaced_with_last_entity(self) -> None:
        ctx = ConversationContext(turns=(turn("average rpm of truck-102 yesterday?"),))

        resolved = resolve_followup("what about its speed today?", ctx, ("speed", "rpm"))

        assert "truck-102" in resolved
        assert " its " not in resolved

    def test_entity_only_followup_gains_last_metric(self) -> None:
        ctx = ConversationContext(turns=(turn("average rpm of truck-102 yesterday?"),))

        resolved = resolve_followup("what about truck-103?", ctx, ("speed", "rpm"))

        assert "truck-103" in resolved
        assert "rpm" in resolved

    def test_question_with_own_entity_and_metric_is_untouched(self) -> None:
        ctx = ConversationContext(turns=(turn("average rpm of truck-102?"),))

        resolved = resolve_followup("max speed of truck-103 today?", ctx, ("speed", "rpm"))

        assert resolved == "max speed of truck-103 today?"

    def test_empty_context_passthrough(self) -> None:
        resolved = resolve_followup("average speed?", ConversationContext(turns=()), ("speed",))

        assert resolved == "average speed?"


class TestAssumeFirst:
    def test_missing_time_window_assumes_default_with_note(self) -> None:
        result = assume_first(
            question="average speed of truck-102",
            resolved_keys=("speed",),
            key_aggregations={"speed": "average"},
            defaults={"*": "average"},
            default_window="today",
        )

        assert isinstance(result, Assumptions)
        assert result.question != "average speed of truck-102"
        assert "today" in result.question
        assert "today" in (result.note or "")

    def test_explicit_time_is_not_assumed(self) -> None:
        result = assume_first(
            question="average speed of truck-102 yesterday",
            resolved_keys=("speed",),
            key_aggregations={"speed": "average"},
            defaults={"*": "average"},
            default_window="today",
        )

        assert "today" not in result.question.lower()

    def test_glob_default_wins_over_star(self) -> None:
        result = assume_first(
            question="battery voltage of truck-102 now-ish",
            resolved_keys=("battery.voltage",),
            key_aggregations={"battery.voltage": "latest"},
            defaults={"*": "average", "battery*": "latest"},
            default_window="today",
        )

        assert "latest" in result.question.lower()

    def test_no_assumptions_needed_yields_no_note(self) -> None:
        result = assume_first(
            question="average speed of truck-102 yesterday",
            resolved_keys=("speed",),
            key_aggregations={"speed": "average"},
            defaults={"*": "average"},
            default_window="today",
        )

        assert result.note is None

    def test_fleet_scope_noted_when_no_entity(self) -> None:
        result = assume_first(
            question="average speed",
            resolved_keys=("speed",),
            key_aggregations={"speed": "average"},
            defaults={"*": "average"},
            default_window="today",
        )

        assert "fleet" in (result.note or "").lower()
