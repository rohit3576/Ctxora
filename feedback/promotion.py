"""Promotion: human review turning feedback into approved few-shot examples."""

import logging
from dataclasses import dataclass
from typing import Final

from feedback.contracts import FeedbackStatus, FeedbackStore
from feedback.hooks import invalidate_knowledge, normalize_sql
from llm.client import LLMClient

_logger = logging.getLogger("datamind.promotion")

_PROMOTABLE: Final[tuple[FeedbackStatus, ...]] = ("pending", "auto_pending", "approved")
_REVIEWABLE: Final[tuple[FeedbackStatus, ...]] = ("pending", "auto_pending", "review")


@dataclass(frozen=True, slots=True)
class ApproveResult:
    """Outcome of one approve action."""

    approved: bool
    promoted: bool
    action: str
    feedback_id: int
    example_tenant: str | None = None


@dataclass(frozen=True, slots=True)
class RejectResult:
    """Outcome of one reject action."""

    rejected: bool
    feedback_id: int


def approve(
    feedback: FeedbackStore, llm: LLMClient, embedding_model: str, feedback_id: int, reviewer: str
) -> ApproveResult:
    """Approve one signal and promote its pair into the example table."""
    row = feedback.get(feedback_id)
    if row is None or row.status not in _PROMOTABLE:
        return ApproveResult(
            approved=False,
            promoted=False,
            action=f"not promotable in status {row.status if row else 'missing'}",
            feedback_id=feedback_id,
        )

    sql_to_promote = row.corrected_sql or row.generated_sql
    embedding = llm.embed([row.nl_query])[0]
    feedback.upsert_approved_example(
        tenant=row.tenant,
        question=row.nl_query,
        sql=sql_to_promote,
        embedding=embedding,
        provenance_feedback_id=feedback_id,
        embedding_model=embedding_model,
    )
    feedback.set_status(feedback_id, "approved", reviewed_by=reviewer)
    invalidate_knowledge(row.tenant)
    _logger.info(
        "feedback %s approved by %s; example promoted for %s", feedback_id, reviewer, row.tenant
    )
    return ApproveResult(
        approved=True,
        promoted=True,
        action="approved",
        feedback_id=feedback_id,
        example_tenant=row.tenant,
    )


def reject(feedback: FeedbackStore, feedback_id: int, reviewer: str) -> RejectResult:
    """Reject one signal (no example changes)."""
    row = feedback.get(feedback_id)
    if row is None or row.status not in _REVIEWABLE:
        return RejectResult(rejected=False, feedback_id=feedback_id)
    feedback.set_status(feedback_id, "rejected", reviewed_by=reviewer)
    return RejectResult(rejected=True, feedback_id=feedback_id)


def auto_promote_positive(
    feedback: FeedbackStore, llm: LLMClient, embedding_model: str, reviewer: str
) -> int:
    """Batch-approve every pending positive signal; returns promoted count."""
    pending = feedback.list_by_status(("pending",))
    promoted = 0
    for row in pending:
        if row.feedback_type != "positive":
            continue
        result = approve(feedback, llm, embedding_model, row.id, reviewer)
        if result.approved:
            promoted += 1
    return promoted


def decay_matches(feedback: FeedbackStore, tenant: str, sql: str) -> int:
    """Demote approved examples whose SQL matches; returns demoted count."""
    target = normalize_sql(sql)
    demoted = 0
    for example_id, example_sql in feedback.approved_example_sqls(tenant):
        if normalize_sql(example_sql) == target:
            feedback.demote_example(example_id)
            demoted += 1
    return demoted
