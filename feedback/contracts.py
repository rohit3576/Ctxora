"""Feedback contracts: rows, inserts, and the FeedbackStore protocol."""

import datetime
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

FeedbackStatus = Literal["pending", "auto_pending", "approved", "rejected", "review"]
Rating = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class FeedbackInsert:
    """One feedback signal about to be persisted."""

    tenant: str
    nl_query: str
    generated_sql: str
    feedback_type: Literal["positive", "negative"]
    session_id: str | None = None
    history_id: int | None = None
    user_comment: str | None = None
    corrected_sql: str | None = None
    status: FeedbackStatus = "pending"
    correction_delta: dict[str, list[str]] | None = None


@dataclass(frozen=True, slots=True)
class FeedbackRow:
    """One persisted feedback signal."""

    id: int
    tenant: str
    nl_query: str
    generated_sql: str
    feedback_type: str
    status: FeedbackStatus
    session_id: str | None = None
    history_id: int | None = None
    user_comment: str | None = None
    corrected_sql: str | None = None
    reviewed_by: str | None = None
    created_at: datetime.datetime | None = None
    correction_delta: dict[str, list[str]] | None = None


@dataclass(frozen=True, slots=True)
class GoldenRow:
    """One approved question/SQL pair exported for regression use."""

    question: str
    sql: str
    tenant: str


@runtime_checkable
class FeedbackStore(Protocol):
    """Persistence + example-table promotion access for the flywheel."""

    def insert(self, row: FeedbackInsert) -> int:
        """Persist one signal; return its id."""
        ...

    def get(self, feedback_id: int) -> FeedbackRow | None:
        """Look up one signal by id."""
        ...

    def list_by_status(
        self, statuses: tuple[FeedbackStatus, ...], tenant: str | None = None, limit: int = 100
    ) -> list[FeedbackRow]:
        """Signals with one of the statuses, newest first."""
        ...

    def set_status(
        self, feedback_id: int, status: FeedbackStatus, reviewed_by: str | None = None
    ) -> bool:
        """Transition one signal's status; False when unknown id."""
        ...

    def stats(self) -> dict[str, int]:
        """Count of signals per status."""
        ...

    def approved_example_sqls(self, tenant: str) -> list[tuple[int, str]]:
        """Approved examples (id, sql) for one tenant."""
        ...

    def demote_example(self, example_id: int) -> None:
        """Mark one approved example for re-review (decay)."""
        ...

    def upsert_approved_example(
        self,
        tenant: str,
        question: str,
        sql: str,
        embedding: list[float],
        provenance_feedback_id: int,
        embedding_model: str,
    ) -> None:
        """Insert or update the approved example for this question."""
        ...

    def golden_rows(self) -> list[GoldenRow]:
        """Approved question/SQL pairs in id order."""
        ...
