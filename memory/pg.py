"""PostgreSQL memory store over an injected query executor."""

import datetime
import json
import uuid
from typing import Final

from pydantic import TypeAdapter, ValidationError

from database.contracts import JsonScalar
from knowledge.store import Query
from memory.contracts import (
    HistoryTurn,
    Session,
    SessionHistory,
    TurnInsert,
)


def _to_text(value: object) -> str:
    """Narrow a cell to str (empty when not a str)."""
    return value if isinstance(value, str) else ""


def _to_int(value: object) -> int:
    """Narrow a cell to int (0 when not numeric)."""
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


_ROWS_ADAPTER: Final = TypeAdapter(list[dict[str, JsonScalar]])


def _to_rows(value: object) -> list[dict[str, JsonScalar]]:
    """Parse a jsonb cell into typed row dicts ([] when malformed)."""
    try:
        return _ROWS_ADAPTER.validate_python(value)
    except ValidationError:
        return []


class PGMemoryStore:
    """Sessions and history in PostgreSQL; queries via the injected executor."""

    def __init__(self, query: Query) -> None:
        """Bind the (sql, params) -> rows executor (commits writes)."""
        self._query: Query = query

    def create_session(self, tenant: str, title: str) -> Session:
        """Insert one session row with a client-generated UUID."""
        session_id = str(uuid.uuid4())
        now = datetime.datetime.now(tz=datetime.UTC)
        self._query(
            "INSERT INTO llm_sessions (id, tenant, title, created_at) VALUES (%s, %s, %s, %s)",
            (session_id, tenant, title, now),
        )
        return Session(id=session_id, tenant=tenant, title=title, created_at=now)

    def fetch_session(self, session_id: str) -> Session | None:
        """Look up one session by id."""
        rows = self._query(
            "SELECT id, tenant, title, user_email, created_at FROM llm_sessions WHERE id = %s",
            (session_id,),
        )
        if not rows:
            return None
        row = rows[0]
        created = row[4] if isinstance(row[4], datetime.datetime) else None
        return Session(
            id=_to_text(row[0]),
            tenant=_to_text(row[1]),
            title=_to_text(row[2]),
            user_email=_to_text(row[3]) or None,
            created_at=created,
        )

    def append_turn(self, turn: TurnInsert) -> int:
        """Insert one history row; return the generated id."""
        rows = self._query(
            "INSERT INTO llm_sql_history "
            "(session_id, tenant, nl_query, sql, data, summary, token_usage, supersedes_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                turn.session_id,
                turn.tenant,
                turn.nl_query,
                turn.sql,
                json.dumps([dict(row) for row in turn.data]),
                turn.summary,
                turn.token_usage,
                turn.supersedes_id,
            ),
        )
        return _to_int(rows[0][0])

    def find_turn(self, history_id: int) -> HistoryTurn | None:
        """Look up one turn by its history id."""
        rows = self._query(
            "SELECT id, session_id, nl_query, sql, data, summary, token_usage, "
            "supersedes_id, created_at FROM llm_sql_history WHERE id = %s",
            (history_id,),
        )
        if not rows:
            return None
        row = rows[0]
        return HistoryTurn(
            id=_to_int(row[0]),
            session_id=_to_text(row[1]),
            nl_query=_to_text(row[2]),
            sql=_to_text(row[3]),
            data=tuple(_to_rows(row[4])),
            summary=_to_text(row[5]),
            token_usage=_to_int(row[6]),
            supersedes_id=_to_int(row[7]) or None,
            created_at=row[8] if isinstance(row[8], datetime.datetime) else None,
        )

    def list_history(self, tenant: str, limit: int = 100) -> list[SessionHistory]:
        """Sessions newest-first with their oldest-first turns."""
        session_rows = self._query(
            "SELECT id, tenant, title, user_email, created_at FROM llm_sessions "
            "WHERE tenant = %s ORDER BY created_at DESC LIMIT %s",
            (tenant, limit),
        )
        sessions = [
            Session(
                id=_to_text(row[0]),
                tenant=_to_text(row[1]),
                title=_to_text(row[2]),
                user_email=_to_text(row[3]) or None,
                created_at=row[4] if isinstance(row[4], datetime.datetime) else None,
            )
            for row in session_rows
        ]
        if not sessions:
            return []
        placeholders = ", ".join(f"'{s.id}'" for s in sessions)
        turn_rows = self._query(
            f"SELECT id, session_id, nl_query, sql, data, summary, token_usage, "  # noqa: S608
            f"supersedes_id, created_at "
            f"FROM llm_sql_history WHERE session_id IN ({placeholders}) "
            f"ORDER BY id",
            (),
        )
        turns_by_session: dict[str, list[HistoryTurn]] = {}
        for row in turn_rows:
            turns_by_session.setdefault(_to_text(row[1]), []).append(
                HistoryTurn(
                    id=_to_int(row[0]),
                    session_id=_to_text(row[1]),
                    nl_query=_to_text(row[2]),
                    sql=_to_text(row[3]),
                    data=tuple(_to_rows(row[4])),
                    summary=_to_text(row[5]),
                    token_usage=_to_int(row[6]),
                    supersedes_id=_to_int(row[7]) or None,
                    created_at=row[8] if isinstance(row[8], datetime.datetime) else None,
                )
            )
        return [
            SessionHistory(session=s, turns=tuple(turns_by_session.get(s.id, ()))) for s in sessions
        ]
