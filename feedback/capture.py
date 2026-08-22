"""Capture: thumbs up/down against a history turn, tenant derived server-side."""

from dataclasses import dataclass

from feedback.contracts import FeedbackInsert, FeedbackStore, Rating
from memory.contracts import MemoryStore


class HistoryNotFoundError(Exception):
    """The referenced history turn (or its session) does not exist."""

    def __init__(self, history_id: int) -> None:
        """Name the missing history id."""
        self.history_id: int = history_id
        super().__init__(f"history turn {history_id} not found")


@dataclass(frozen=True, slots=True)
class CapturedFeedback:
    """Outcome of one capture: the stored row id under the derived tenant."""

    feedback_id: int
    tenant: str
    rating: Rating


def capture(
    feedback: FeedbackStore,
    memory: MemoryStore,
    history_id: int,
    rating: Rating,
    *,
    session_id: str | None = None,
    comment: str | None = None,
) -> CapturedFeedback:
    """Store one signal; tenant/question/sql come from the stored turn.

    Raises:
        HistoryNotFoundError: history_id (or its session) is unknown.
    """
    turn = memory.find_turn(history_id)
    if turn is None:
        raise HistoryNotFoundError(history_id)
    session = memory.fetch_session(turn.session_id)
    if session is None:
        raise HistoryNotFoundError(history_id)

    feedback_id = feedback.insert(
        FeedbackInsert(
            tenant=session.tenant,
            nl_query=turn.nl_query,
            generated_sql=turn.sql,
            feedback_type="positive" if rating == "up" else "negative",
            session_id=session_id,
            history_id=history_id,
            user_comment=comment,
            status="pending",
        )
    )
    return CapturedFeedback(feedback_id=feedback_id, tenant=session.tenant, rating=rating)
