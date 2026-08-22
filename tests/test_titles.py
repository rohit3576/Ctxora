"""Title tests: deterministic keyword titles from config."""

from agent.titles import title_for
from config.settings import AgentConfig

KEYWORDS = AgentConfig().title_keywords


class TestTitleFor:
    def test_keyword_hit_becomes_title_case_query(self) -> None:
        assert title_for("What was the average rpm yesterday?", KEYWORDS) == "Rpm Query"

    def test_first_keyword_in_config_order_wins(self) -> None:
        title = title_for("battery and fuel this week", KEYWORDS)

        assert title == "Battery Query"

    def test_word_boundary_prevents_substring_match(self) -> None:
        assert title_for("speedometer is odd", KEYWORDS) == "Telemetry Query"

    def test_no_keyword_falls_back_to_generic(self) -> None:
        assert title_for("hello there", KEYWORDS) == "Telemetry Query"

    def test_case_insensitive_match(self) -> None:
        assert title_for("SPEED of the fleet", KEYWORDS) == "Speed Query"
