"""In-memory FeedbackStore fake: real behavior, dict-backed."""

import datetime

from feedback.contracts import FeedbackInsert, FeedbackRow, FeedbackStatus, GoldenRow


class InMemoryFeedbackStore:
    """Dict-backed feedback store for tests and local demos."""

    def __init__(self) -> None:
        """Start empty with an isolated id sequence."""
        self.rows: dict[int, FeedbackRow] = {}
        self.examples: dict[int, dict[str, object]] = {}
        self._next_id: int = 1
        self._next_example_id: int = 1

    def insert(self, row: FeedbackInsert) -> int:
        """Persist one signal; return its id."""
        feedback_id = self._next_id
        self._next_id += 1
        self.rows[feedback_id] = FeedbackRow(
            id=feedback_id,
            tenant=row.tenant,
            nl_query=row.nl_query,
            generated_sql=row.generated_sql,
            feedback_type=row.feedback_type,
            status=row.status,
            session_id=row.session_id,
            history_id=row.history_id,
            user_comment=row.user_comment,
            corrected_sql=row.corrected_sql,
            created_at=datetime.datetime.now(tz=datetime.UTC),
            correction_delta=dict(row.correction_delta) if row.correction_delta else None,
        )
        return feedback_id

    def get(self, feedback_id: int) -> FeedbackRow | None:
        """Look up one signal by id."""
        return self.rows.get(feedback_id)

    def list_by_status(
        self,
        statuses: tuple[FeedbackStatus, ...],
        tenant: str | None = None,
        limit: int = 100,
    ) -> list[FeedbackRow]:
        """Signals with one of the statuses, newest first."""
        matched = [
            row
            for row in self.rows.values()
            if row.status in statuses and (tenant is None or row.tenant == tenant)
        ]
        matched.sort(key=lambda r: r.id, reverse=True)
        return matched[:limit]

    def set_status(
        self, feedback_id: int, status: FeedbackStatus, reviewed_by: str | None = None
    ) -> bool:
        """Transition one signal's status; False when unknown id."""
        row = self.rows.get(feedback_id)
        if row is None:
            return False
        self.rows[feedback_id] = FeedbackRow(
            id=row.id,
            tenant=row.tenant,
            nl_query=row.nl_query,
            generated_sql=row.generated_sql,
            feedback_type=row.feedback_type,
            status=status,
            session_id=row.session_id,
            history_id=row.history_id,
            user_comment=row.user_comment,
            corrected_sql=row.corrected_sql,
            reviewed_by=reviewed_by,
            created_at=row.created_at,
        )
        return True

    def stats(self) -> dict[str, int]:
        """Count of signals per status."""
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    def approved_example_sqls(self, tenant: str) -> list[tuple[int, str]]:
        """Approved examples (id, sql) for one tenant."""
        return [
            (_as_int(example["id"]), str(example["sql"]))
            for example in self.examples.values()
            if example["tenant"] == tenant and example["status"] == "approved"
        ]

    def demote_example(self, example_id: int) -> None:
        """Mark one approved example for re-review (decay)."""
        example = self.examples.get(example_id)
        if example is None:
            return
        example["status"] = "review"
        example["corrections_after_use"] = _as_int(example.get("corrections_after_use", 0)) + 1

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
        for example in self.examples.values():
            if example["tenant"] == tenant and example["question"] == question:
                example.update(
                    {
                        "sql": sql,
                        "embedding": embedding,
                        "status": "approved",
                        "provenance_feedback_id": provenance_feedback_id,
                        "embedding_model": embedding_model,
                    }
                )
                return
        example_id = self._next_example_id
        self._next_example_id += 1
        self.examples[example_id] = {
            "id": example_id,
            "tenant": tenant,
            "question": question,
            "sql": sql,
            "embedding": embedding,
            "status": "approved",
            "provenance_feedback_id": provenance_feedback_id,
            "embedding_model": embedding_model,
            "corrections_after_use": 0,
        }

    def golden_rows(self) -> list[GoldenRow]:
        """Approved question/SQL pairs in id order."""
        return [
            GoldenRow(
                question=row.nl_query, sql=row.corrected_sql or row.generated_sql, tenant=row.tenant
            )
            for row in sorted(self.rows.values(), key=lambda r: r.id)
            if row.status == "approved"
        ]


def _as_int(value: object) -> int:
    """Narrow a cell to int (0 when absent)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)
    return 0
