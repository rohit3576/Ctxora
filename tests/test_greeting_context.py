"""Greeting + conversation-context tests."""

from agent.conversation import ConversationContext, last_entity, last_metric
from agent.greeting import is_greeting
from memory.contracts import HistoryTurn


def turn(q: str) -> HistoryTurn:
    """One minimal history turn."""
    return HistoryTurn(
        id=1, session_id="s", nl_query=q, sql="SELECT 1", data=(), summary="", token_usage=0
    )


class TestIsGreeting:
    def test_common_greetings_match(self) -> None:
        for phrase in ("hi", "hello there", "hey", "good morning", "thanks!", "bye"):
            assert is_greeting(phrase) is True

    def test_data_questions_do_not_match(self) -> None:
        for phrase in ("average rpm yesterday", "hi-speed truck data", "what happened at 9"):
            assert is_greeting(phrase) is False


class TestLastEntity:
    def test_extracts_device_token(self) -> None:
        ctx = ConversationContext(turns=(turn("average rpm of truck-102 yesterday?"),))

        assert last_entity(ctx) == "truck-102"

    def test_none_without_entities(self) -> None:
        ctx = ConversationContext(turns=(turn("average fleet speed?"),))

        assert last_entity(ctx) is None

    def test_uses_most_recent_turn_first(self) -> None:
        ctx = ConversationContext(turns=(turn("rpm of truck-101?"), turn("speed of truck-102?")))

        assert last_entity(ctx) == "truck-102"

    def test_empty_context_is_none(self) -> None:
        assert last_entity(ConversationContext(turns=())) is None


class TestLastMetric:
    def test_extracts_alias_word(self) -> None:
        ctx = ConversationContext(turns=(turn("average rpm of truck-102?"),))

        assert last_metric(ctx, ("rpm", "speed")) == "rpm"

    def test_none_when_no_alias_present(self) -> None:
        ctx = ConversationContext(turns=(turn("weather?"),))

        assert last_metric(ctx, ("rpm", "speed")) is None
