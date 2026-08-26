"""Session-aware query rewriting: follow-up questions -> standalone retrieval queries.

R2 of the RAG upgrade plan. The rewrite is an enhancement, never a dependency:
no turns, LLM failure, or degenerate output all degrade to the raw question.
"""

from collections.abc import Sequence
from typing import Final

from llm.client import LLMClient
from llm.openai_compat import LLMError

_SYSTEM: Final = (
    "You rewrite a follow-up question into a standalone query for document search. "
    "Merge every device name, error code, part number, and metric from the recent "
    "conversation into the question so it can be understood with no other context. "
    "Reply with ONLY the rewritten query on one line, no quotes, no explanation. "
    "If the question is already self-contained, reply with it unchanged."
)
_MAX_REWRITE_CHARS: Final = 2000


def rewrite_query(llm: LLMClient, question: str, recent_turns: Sequence[str]) -> str:
    """Self-contained retrieval query from question + recent turns.

    Empty turns -> question unchanged (callers should pre-filter, this is the
    hard guard). LLM failure or empty/oversized output -> question unchanged.
    """
    if not recent_turns:
        return question
    conversation = "\n".join(f"- {turn}" for turn in recent_turns)
    user = f"RECENT CONVERSATION:\n{conversation}\n\nQUESTION: {question}"
    try:
        result = llm.generate(_SYSTEM, user, temperature=0.0)
    except LLMError:
        return question
    rewritten = result.raw.strip()
    if not rewritten or len(rewritten) > _MAX_REWRITE_CHARS:
        return question
    return rewritten
