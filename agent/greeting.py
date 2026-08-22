"""Greeting detection: zero-LLM conversational shortcut."""

import re

_GREETING: re.Pattern[str] = re.compile(
    r"^\s*(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks(?: a lot)?|"
    r"thank you|bye|goodbye)[!,. ]*(?:\s+(?:there|you|all|everyone|team))*[!,. ]*$",
    re.IGNORECASE,
)

REPLY = "Hello! Ask me about your telemetry — for example: 'average RPM of truck-102 yesterday?'"


def is_greeting(question: str) -> bool:
    """Whole-message greetings only; embedded words never match."""
    return _GREETING.match(question.strip()) is not None
