"""Post-correction hooks: mine the repair, decay contradicted examples."""

import logging

from agent.pipeline import QuerySuccess
from feedback.contracts import FeedbackInsert, FeedbackStore
from feedback.normalize import normalize_sql
from feedback.similarity import correction_delta, similar
from knowledge.store import KnowledgeStore

_logger = logging.getLogger("ctxora.feedback_hooks")

__all__ = ["after_correction", "invalidate_knowledge", "normalize_sql"]


def after_correction(
    feedback: FeedbackStore,
    tenant: str,
    question: str,
    success: QuerySuccess,
    *,
    previous_sql: str | None = None,
    history_id: int | None = None,
    structural: bool = False,
) -> None:
    """Mine one successful correction and decay examples it contradicts.

    Non-blocking by contract: callers wrap in their own boundary handling;
    this function logs and swallows nothing on its own. The correction is
    also mined into labeled deltas (what the fix teaches); mining failure
    degrades to no delta, never a failed answer. structural widens decay
    matching from exact to shape equality.
    """
    if history_id is None:
        return
    delta = None
    if previous_sql:
        try:
            delta = correction_delta(previous_sql, success.sql)
        except Exception as exc:  # noqa: BLE001 (boundary: mining must never break answering)
            _logger.warning("correction delta mining failed (non-blocking): %s", exc)
    feedback.insert(
        FeedbackInsert(
            tenant=tenant,
            nl_query=question,
            generated_sql=success.sql,
            feedback_type="positive",
            history_id=history_id,
            corrected_sql=success.sql,
            status="auto_pending",
            correction_delta=delta,
        )
    )
    if previous_sql:
        _decay(feedback, tenant, previous_sql, structural=structural)


def _decay(
    feedback: FeedbackStore, tenant: str, previous_sql: str, structural: bool = False
) -> None:
    """Demote approved examples matching the SQL a user just corrected.

    structural=True demotes every shape-equal example (alias renames and
    column reorder count as the same bad SQL), not just exact matches.
    """
    superseded = normalize_sql(previous_sql)
    for example_id, example_sql in feedback.approved_example_sqls(tenant):
        matched = normalize_sql(example_sql) == superseded or (
            structural and similar(example_sql, previous_sql)
        )
        if matched:
            feedback.demote_example(example_id)
            _logger.info("example %s demoted to review (matched corrected SQL)", example_id)


def invalidate_knowledge(tenant: str) -> None:
    """Drop the tenant's cached knowledge after example-table writes."""
    KnowledgeStore.invalidate_cache(tenant)
