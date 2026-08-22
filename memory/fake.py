"""In-memory MemoryStore fake: real behavior, dict-backed."""

import datetime
import uuid

from memory.contracts import (
    HistoryTurn,
    Session,
    SessionHistory,
    TurnInsert,
)

_URN = uuid.UUID("00000000-0000-0000-0000-000000000000")


class InMemoryMemoryStore:
    """Dict-backed memory store for tests and local demos."""

    def __init__(self) -> None:
        """Start empty with an isolated id sequence."""
        self.sessions: dict[str, Session] = {}
        self.turns: list[HistoryTurn] = []
        self._next_turn_id: int = 1
        self._creation_seq: int = 0
        self.session_order: dict[str, int] = {}

    def create_session(self, tenant: str, title: str) -> Session:
        """Create one session with a fresh UUID (now-stamped)."""
        session = Session(
            id=str(uuid.uuid4()),
            tenant=tenant,
            title=title,
            created_at=datetime.datetime.now(tz=datetime.UTC),
        )
        self._creation_seq += 1
        self.sessions[session.id] = session
        self.session_order[session.id] = self._creation_seq
        return session

    def fetch_session(self, session_id: str) -> Session | None:
        """Look up one session by id."""
        return self.sessions.get(session_id)

    def append_turn(self, turn: TurnInsert) -> int:
        """Persist one turn; return its monotonic id."""
        turn_id = self._next_turn_id
        self._next_turn_id += 1
        self.turns.append(
            HistoryTurn(
                id=turn_id,
                session_id=turn.session_id,
                nl_query=turn.nl_query,
                sql=turn.sql,
                data=turn.data,
                summary=turn.summary,
                token_usage=turn.token_usage,
                supersedes_id=turn.supersedes_id,
                created_at=datetime.datetime.now(tz=datetime.UTC),
            )
        )
        return turn_id

    def find_turn(self, history_id: int) -> HistoryTurn | None:
        """Look up one turn by its history id."""
        for turn in self.turns:
            if turn.id == history_id:
                return turn
        return None

    def list_history(self, tenant: str, limit: int = 100) -> list[SessionHistory]:
        """Sessions newest-first with their oldest-first turns."""
        tenant_sessions = [
            session for session in self.sessions.values() if session.tenant == tenant
        ]
        tenant_sessions.sort(
            key=lambda s: (
                s.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
                self.session_order.get(s.id, 0),
            ),
            reverse=True,
        )
        page: list[SessionHistory] = []
        for session in tenant_sessions[:limit]:
            turns = tuple(turn for turn in self.turns if turn.session_id == session.id)
            page.append(SessionHistory(session=session, turns=turns))
        return page
