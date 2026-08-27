"""PostgreSQL FeedbackStore over an injected query executor."""

import datetime
import json
from typing import cast

from feedback.contracts import (
    FeedbackInsert,
    FeedbackRow,
    FeedbackStatus,
    GoldenRow,
)
from knowledge.store import Query

_PROMOTABLE: tuple[FeedbackStatus, ...] = ("pending", "auto_pending", "approved")


def _text(value: object) -> str:
    """Narrow a cell to str (empty when not a str)."""
    return value if isinstance(value, str) else ""


def _opt_int(value: object) -> int | None:
    """Narrow a cell to int (None when absent)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _delta_of(value: object) -> dict[str, list[str]] | None:
    """Narrow a JSONB cell to the labeled-delta map."""
    if not isinstance(value, dict):
        return None
    cell: dict[str, list[str]] = {}
    for key, items in cast("dict[str, list[str]]", value).items():
        cell[str(key)] = [str(item) for item in items]
    return cell or None


def _row_of(row: tuple[object, ...]) -> FeedbackRow:
    """Shape one SELECT row into a FeedbackRow."""
    status = _text(row[5])
    return FeedbackRow(
        id=_opt_int(row[0]) or 0,
        tenant=_text(row[1]),
        nl_query=_text(row[2]),
        generated_sql=_text(row[3]),
        feedback_type=_text(row[4]),
        status=status if status in _PROMOTABLE or status in ("rejected", "review") else "pending",
        session_id=_text(row[6]) or None,
        history_id=_opt_int(row[7]),
        user_comment=_text(row[8]) or None,
        corrected_sql=_text(row[9]) or None,
        reviewed_by=_text(row[10]) or None,
        created_at=row[11] if isinstance(row[11], datetime.datetime) else None,
        correction_delta=_delta_of(row[12]) if len(row) > 12 else None,
    )


_SELECT_COLUMNS = (
    "id, tenant, nl_query, generated_sql, feedback_type, status, "
    "session_id, history_id, user_comment, corrected_sql, reviewed_by, created_at, "
    "correction_delta"
)


class PGFeedbackStore:
    """query_feedback + example promotion tables in PostgreSQL."""

    def __init__(self, query: Query) -> None:
        """Bind the (sql, params) -> rows executor (commits writes)."""
        self._query: Query = query

    def insert(self, row: FeedbackInsert) -> int:
        """Persist one signal; return the generated id."""
        rows = self._query(
            "INSERT INTO query_feedback "
            "(tenant, session_id, history_id, nl_query, generated_sql, feedback_type, "
            "user_comment, corrected_sql, status, correction_delta) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (
                row.tenant,
                row.session_id,
                row.history_id,
                row.nl_query,
                row.generated_sql,
                row.feedback_type,
                row.user_comment,
                row.corrected_sql,
                row.status,
                json.dumps(row.correction_delta) if row.correction_delta else None,
            ),
        )
        return _opt_int(rows[0][0]) or 0

    def get(self, feedback_id: int) -> FeedbackRow | None:
        """Look up one signal by id."""
        rows = self._query(
            f"SELECT {_SELECT_COLUMNS} FROM query_feedback WHERE id = %s",  # noqa: S608
            (feedback_id,),
        )
        return _row_of(rows[0]) if rows else None

    def list_by_status(
        self,
        statuses: tuple[FeedbackStatus, ...],
        tenant: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRow]:
        """Signals with one of the statuses, newest first."""
        placeholders = ", ".join(f"'{status}'" for status in statuses)
        where_tenant = " AND tenant = %s" if tenant else ""
        params: tuple[object, ...] = (tenant, limit) if tenant else (limit,)
        rows = self._query(
            f"SELECT {_SELECT_COLUMNS} FROM query_feedback "  # noqa: S608
            f"WHERE status IN ({placeholders}){where_tenant} "
            f"ORDER BY id DESC LIMIT %s",
            params,
        )
        return [_row_of(row) for row in rows]

    def set_status(
        self, feedback_id: int, status: FeedbackStatus, reviewed_by: str | None = None
    ) -> bool:
        """Transition one signal's status; False when unknown id."""
        rows = self._query(
            "UPDATE query_feedback SET status = %s, reviewed_by = %s, reviewed_at = NOW() "
            "WHERE id = %s RETURNING id",
            (status, reviewed_by, feedback_id),
        )
        return bool(rows)

    def stats(self) -> dict[str, int]:
        """Count of signals per status."""
        rows = self._query("SELECT status, count(*) FROM query_feedback GROUP BY status", ())
        return {_text(row[0]): _opt_int(row[1]) or 0 for row in rows}

    def approved_example_sqls(self, tenant: str) -> list[tuple[int, str]]:
        """Approved examples (id, sql) for one tenant."""
        rows = self._query(
            "SELECT s.id, s.sql_query FROM sql_agent_sql_examples s "
            "JOIN sql_agent_tenants t ON t.id = s.tenant_id "
            "WHERE t.tenant_name = %s AND s.status = 'approved' ORDER BY s.id",
            (tenant,),
        )
        return [(_opt_int(row[0]) or 0, _text(row[1])) for row in rows]

    def demote_example(self, example_id: int) -> None:
        """Mark one approved example for re-review (decay)."""
        self._query(
            "UPDATE sql_agent_sql_examples SET status = 'review', "
            "corrections_after_use = corrections_after_use + 1 WHERE id = %s",
            (example_id,),
        )

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
        vector = "[" + ",".join(str(value) for value in embedding) + "]"
        self._query(
            "INSERT INTO sql_agent_sql_examples "
            "(tenant_id, question, sql_query, embedding, status, "
            "provenance_feedback_id, embedding_model) "
            "SELECT t.id, %s, %s, %s::vector, 'approved', %s, %s "
            "FROM sql_agent_tenants t WHERE t.tenant_name = %s "
            "ON CONFLICT (tenant_id, question) DO UPDATE SET "
            "sql_query = EXCLUDED.sql_query, embedding = EXCLUDED.embedding, "
            "status = 'approved', provenance_feedback_id = EXCLUDED.provenance_feedback_id, "
            "embedding_model = EXCLUDED.embedding_model",
            (question, sql, vector, provenance_feedback_id, embedding_model, tenant),
        )

    def golden_rows(self) -> list[GoldenRow]:
        """Approved question/SQL pairs in id order."""
        rows = self._query(
            "SELECT tenant, nl_query, COALESCE(corrected_sql, generated_sql) "
            "FROM query_feedback WHERE status = 'approved' ORDER BY id",
            (),
        )
        return [
            GoldenRow(tenant=_text(row[0]), question=_text(row[1]), sql=_text(row[2]))
            for row in rows
        ]
