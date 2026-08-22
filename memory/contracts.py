"""Memory contracts: sessions, turns, and the MemoryStore protocol."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from database.contracts import JsonScalar


@dataclass(frozen=True, slots=True)
class Session:
    """One conversation session."""

    id: str
    tenant: str
    title: str
    user_email: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TurnInsert:
    """One answered query about to be persisted."""

    tenant: str
    session_id: str
    nl_query: str
    sql: str
    data: tuple[dict[str, JsonScalar], ...]
    summary: str
    token_usage: int
    supersedes_id: int | None = None


@dataclass(frozen=True, slots=True)
class HistoryTurn:
    """One persisted query turn."""

    id: int
    session_id: str
    nl_query: str
    sql: str
    data: tuple[dict[str, JsonScalar], ...]
    summary: str
    token_usage: int
    supersedes_id: int | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SessionHistory:
    """One session with its turns, oldest turn first."""

    session: Session
    turns: tuple[HistoryTurn, ...]


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence for sessions and query history."""

    def create_session(self, tenant: str, title: str) -> Session:
        """Create and persist one session."""
        ...

    def fetch_session(self, session_id: str) -> Session | None:
        """Look up one session by id."""
        ...

    def append_turn(self, turn: TurnInsert) -> int:
        """Persist one answered turn; return its id."""
        ...

    def find_turn(self, history_id: int) -> HistoryTurn | None:
        """Look up one turn by its history id."""
        ...

    def list_history(self, tenant: str, limit: int = 100) -> list[SessionHistory]:
        """Sessions newest-first with their oldest-first turns."""
        ...
