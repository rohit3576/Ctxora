"""Post-correction hooks: mine the repair, decay contradicted examples."""

import logging
import re

from agent.pipeline import QuerySuccess
from feedback.contracts import FeedbackInsert, FeedbackStore
from knowledge.store import KnowledgeStore

_logger = logging.getLogger("datamind.feedback_hooks")


def normalize_sql(sql: str) -> str:
    """Canonical form for SQL comparison: single-spaced, lowercased."""
    return re.sub(r"\s+", " ", sql).strip().lower()


def after_correction(
    feedback: FeedbackStore,
    tenant: str,
    question: str,
    success: QuerySuccess,
    *,
    previous_sql: str | None = None,
    history_id: int | None = None,
) -> None:
    """Mine one successful correction and decay examples it contradicts.

    Non-blocking by contract: callers wrap in their own boundary handling;
    this function logs and swallows nothing on its own.
    """
    if history_id is None:
        return
    feedback.insert(
        FeedbackInsert(
            tenant=tenant,
            nl_query=question,
            generated_sql=success.sql,
            feedback_type="positive",
            history_id=history_id,
            corrected_sql=success.sql,
            status="auto_pending",
        )
    )
    if previous_sql:
        _decay(feedback, tenant, previous_sql)


def _decay(feedback: FeedbackStore, tenant: str, previous_sql: str) -> None:
    """Demote approved examples matching the SQL a user just corrected."""
    superseded = normalize_sql(previous_sql)
    for example_id, example_sql in feedback.approved_example_sqls(tenant):
        if normalize_sql(example_sql) == superseded:
            feedback.demote_example(example_id)
            _logger.info("example %s demoted to review (matched corrected SQL)", example_id)


def invalidate_knowledge(tenant: str) -> None:
    """Drop the tenant's cached knowledge after example-table writes."""
    KnowledgeStore.invalidate_cache(tenant)
