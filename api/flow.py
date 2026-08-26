"""Shared request flow: session resolution + non-blocking turn recording."""

import logging
from dataclasses import dataclass

from agent.conversation import ConversationContext
from agent.pipeline import QuerySuccess
from agent.titles import title_for
from config.settings import AppConfig
from memory.contracts import MemoryStore, Session, TurnInsert

_logger = logging.getLogger("ctxora.session_flow")


@dataclass(frozen=True, slots=True)
class SessionResolution:
    """Resolved session for one request (None when memory is unavailable)."""

    session: Session | None
    error: str | None


def resolve_session(
    memory: MemoryStore,
    tenant: str,
    question: str,
    session_id: str | None,
    config: AppConfig,
) -> SessionResolution:
    """Find or create the conversation session; never raise."""
    try:
        if session_id is not None:
            existing = memory.fetch_session(session_id)
            if existing is None:
                return SessionResolution(None, f"session not found: {session_id}")
            if existing.tenant != tenant:
                return SessionResolution(None, "session does not belong to this tenant")
            return SessionResolution(existing, None)
        title = title_for(question, config.agent.title_keywords)
        return SessionResolution(memory.create_session(tenant, title), None)
    except Exception as exc:  # noqa: BLE001 (boundary: memory must never break answering)
        _logger.warning("session resolution failed (non-blocking): %s", exc)
        return SessionResolution(None, None)


def conversation_context(memory: MemoryStore, session: Session) -> ConversationContext | None:
    """Load the session's prior turns (non-blocking; None on any failure)."""
    try:
        page = memory.list_history(session.tenant)
    except Exception as exc:  # noqa: BLE001 (boundary: context is best-effort)
        _logger.warning("conversation context load failed (non-blocking): %s", exc)
        return None
    for item in page:
        if item.session.id == session.id:
            return ConversationContext(turns=item.turns)
    return None


def rag_recent_turns(memory: MemoryStore, tenant: str, session_id: str | None) -> list[str]:
    """NL queries of the session's prior turns for RAG rewriting (best-effort).

    Empty when the session is unknown, cross-tenant, or memory fails: the
    rewrite is an enhancement, never a dependency.
    """
    if session_id is None:
        return []
    try:
        session = memory.fetch_session(session_id)
        if session is None or session.tenant != tenant:
            return []
        context = conversation_context(memory, session)
    except Exception as exc:  # noqa: BLE001 (boundary: rewrite context must never break answering)
        _logger.warning("rag session context load failed (non-blocking): %s", exc)
        return []
    if context is None:
        return []
    return [turn.nl_query for turn in context.turns]


def record_turn(
    memory: MemoryStore,
    resolution: SessionResolution,
    tenant: str,
    question: str,
    success: QuerySuccess,
) -> int | None:
    """Persist the answered turn; return its id or None (never raise)."""
    if resolution.session is None:
        return None
    try:
        return memory.append_turn(
            TurnInsert(
                tenant=tenant,
                session_id=resolution.session.id,
                nl_query=question,
                sql=success.sql,
                data=tuple(success.rows),
                summary=success.summary,
                token_usage=success.prompt_tokens + success.completion_tokens,
                supersedes_id=success.supersedes_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 (boundary: memory must never break answering)
        _logger.warning("history write failed (non-blocking): %s", exc)
        return None
