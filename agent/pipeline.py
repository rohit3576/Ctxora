"""Pipeline: S0-S3 conversational front matter + S5-S13 SQL execution.

Stage order: greeting (S0) -> correction (S1) -> follow-up (S2) ->
assume-first (S3) -> key resolution (S5) -> prompt (S7) -> generation (S8)
-> validation (S9) -> execution (S10) -> summary (S12). Every S0-S3 stage
is flag-gated; flags off reproduces Phase 2 behavior exactly.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from agent.assume_first import Assumptions, assume_first
from agent.conversation import ConversationContext
from agent.correction import Clarify, Correction, CorrectionDetector, NotCorrection
from agent.followup import resolve_followup
from agent.generator import SQLGenerator
from agent.greeting import REPLY as GREETING_REPLY
from agent.greeting import is_greeting
from agent.key_resolver import KeyResolver, ResolvedKeys
from agent.prompt_builder import PromptBuilder
from agent.summarizer import Summarizer
from agent.validator import SQLValidator
from config.settings import AppConfig
from database.contracts import JsonScalar, TelemetryStore
from knowledge.contracts import SQLExample, TenantKnowledge
from knowledge.store import KnowledgeStore
from llm.client import LLMClient
from memory.digest import DigestCache

_logger = logging.getLogger("datamind.pipeline")

StageObserver = Callable[[str], None]

STAGE_RETRIEVING = "retrieving"
STAGE_GENERATING = "generating"
STAGE_VALIDATING = "validating"
STAGE_EXECUTING = "executing"
STAGE_SUMMARIZING = "summarizing"

_CORRECTION_TEMPERATURE = 0.3
_SEMANTIC_THRESHOLD = 0.85
_SEMANTIC_LIMIT = 2


@dataclass(frozen=True, slots=True)
class AgentDeps:
    """Everything one pipeline run needs, injected."""

    store: TelemetryStore
    knowledge: KnowledgeStore
    llm: LLMClient
    config: AppConfig


@dataclass(frozen=True, slots=True)
class QuerySuccess:
    """A fully answered query."""

    sql: str
    rows: list[dict[str, JsonScalar]]
    row_count: int
    summary: str
    resolved_keys: tuple[str, ...]
    repairs_applied: tuple[str, ...]
    execution_time_ms: float
    prompt_tokens: int
    completion_tokens: int
    assumption_note: str | None = None
    supersedes_id: int | None = None


@dataclass(frozen=True, slots=True)
class QueryRejected:
    """Validation refused the generated SQL."""

    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueryExecutionFailed:
    """The validated SQL failed at the store."""

    error_kind: str
    error: str


@dataclass(frozen=True, slots=True)
class QueryChitChat:
    """A greeting short-circuit: reply without any SQL work."""

    reply: str


@dataclass(frozen=True, slots=True)
class QueryClarified:
    """The agent asks a targeted question instead of guessing."""

    question: str


QueryOutcome = QuerySuccess | QueryRejected | QueryExecutionFailed | QueryChitChat | QueryClarified


def run_query(
    question: str,
    tenant: str,
    deps: AgentDeps,
    *,
    on_stage: StageObserver | None = None,
    conversation: ConversationContext | None = None,
    digest_cache: DigestCache | None = None,
) -> QueryOutcome:
    """Execute the full pipeline for one question against one tenant."""
    flags = deps.config.flags

    if flags.greeting and is_greeting(question):
        return QueryChitChat(reply=GREETING_REPLY)

    if flags.correction_loop and conversation is not None and conversation.latest is not None:
        outcome = _handle_correction(question, tenant, deps, conversation, on_stage)
        if outcome is not None:
            return outcome

    if flags.followup and conversation is not None and conversation.turns:
        knowledge_probe = deps.knowledge.load(tenant)
        aliases = tuple(entry.alias for entry in knowledge_probe.aliases)
        question = resolve_followup(question, conversation, aliases)

    return _run_sql_flow(
        question,
        tenant,
        deps,
        conversation=conversation,
        digest_cache=digest_cache,
        on_stage=on_stage,
    )


def _handle_correction(
    question: str,
    tenant: str,
    deps: AgentDeps,
    conversation: ConversationContext,
    on_stage: StageObserver | None,
) -> QueryOutcome | None:
    """S1: repair path or clarification; None falls through to a fresh query."""
    previous = conversation.latest
    if previous is None:
        return None

    verdict = CorrectionDetector(llm=deps.llm).detect(question, previous)
    match verdict:
        case NotCorrection():
            return None
        case Clarify() as clarify:
            return QueryClarified(question=clarify.question)
        case Correction(corrected_question=corrected):
            _logger.info("correction accepted, regenerating: %s", corrected)
            repaired = _run_sql_flow(
                corrected,
                tenant,
                deps,
                conversation=conversation,
                on_stage=on_stage,
                temperature=_CORRECTION_TEMPERATURE,
            )
            match repaired:
                case QuerySuccess():
                    return replace(repaired, supersedes_id=previous.id)
                case _:
                    detail = (
                        "I could not produce a corrected query for that. Could you "
                        "restate which entity, which measurement (average, maximum, "
                        "latest), and which day?"
                    )
                    return QueryClarified(question=detail)


def _run_sql_flow(
    question: str,
    tenant: str,
    deps: AgentDeps,
    *,
    conversation: ConversationContext | None = None,
    digest_cache: DigestCache | None = None,
    on_stage: StageObserver | None = None,
    temperature: float = 0.0,
) -> QueryOutcome:
    """S5-S13: resolve keys, build prompt, generate, validate, execute, summarize."""

    def _noop(_stage: str) -> None:
        """Discard stage notifications."""
        return

    observe = on_stage if on_stage is not None else _noop

    observe(STAGE_RETRIEVING)
    knowledge = deps.knowledge.load(tenant)
    resolved = KeyResolver().resolve(question, knowledge)

    question_final, assumption_note = _apply_assume_first(question, resolved, knowledge, deps)

    mapping = deps.config.stores.telemetry.mapping
    allowed_tables = [mapping.table.format(tenant=tenant)]
    events = deps.config.stores.events
    if events.enabled and events.mapping is not None:
        allowed_tables.append(events.mapping.table.format(tenant=tenant))

    digest_text = _digest_text(conversation, digest_cache, deps)
    examples_override = _semantic_examples(question, tenant, deps, knowledge)
    builder = PromptBuilder(dialect=deps.store.dialect, mapping=mapping)
    system, user = builder.build(
        knowledge,
        resolved.keys,
        question_final,
        session_context=digest_text,
        examples_override=examples_override,
    )

    observe(STAGE_GENERATING)
    generated = SQLGenerator(llm=deps.llm).generate(system, user, temperature=temperature)

    observe(STAGE_VALIDATING)
    validator = SQLValidator(
        dialect=deps.store.dialect, mapping=mapping, allowed_tables=tuple(allowed_tables)
    )
    validation = validator.validate(generated.sql)
    if not validation.valid:
        return QueryRejected(errors=validation.errors)

    observe(STAGE_EXECUTING)
    executed = deps.store.execute(
        validation.normalized_sql,
        row_cap=deps.config.agent.row_cap,
        timeout_s=deps.config.agent.query_timeout_s,
    )
    if not executed.success:
        return QueryExecutionFailed(
            error_kind=executed.error_kind or "query",
            error=executed.error or "unknown execution error",
        )

    observe(STAGE_SUMMARIZING)
    rows = [dict(row) for row in executed.rows]
    summary = Summarizer(llm=deps.llm).summarize(question, rows, validation.normalized_sql)
    return QuerySuccess(
        sql=validation.normalized_sql,
        rows=rows,
        row_count=executed.row_count,
        summary=summary.text,
        resolved_keys=tuple(entry.canonical_key for entry in resolved.keys),
        repairs_applied=validation.repairs_applied,
        execution_time_ms=executed.execution_time_ms,
        prompt_tokens=generated.prompt_tokens,
        completion_tokens=generated.completion_tokens,
        assumption_note=assumption_note,
    )


def _semantic_examples(
    question: str,
    tenant: str,
    deps: AgentDeps,
    knowledge: TenantKnowledge,
) -> tuple[SQLExample, ...] | None:
    """Flag-gated cosine retrieval over approved examples; None = keyword path.

    Fail-open: any embedding/store failure logs and returns None so the
    prompt falls back to keyword-sliced examples without user-visible change.
    """
    if not deps.config.flags.semantic_examples:
        return None
    if not knowledge.examples:
        return None
    try:
        embedding = deps.llm.embed([question])[0]
        return tuple(
            deps.knowledge.fetch_semantic_examples(
                tenant=tenant,
                embedding=embedding,
                threshold=_SEMANTIC_THRESHOLD,
                limit=_SEMANTIC_LIMIT,
            )
        )
    except Exception as exc:  # noqa: BLE001 (fail-open to keyword slicing)
        _logger.warning("semantic example retrieval failed (keyword fallback): %s", exc)
        return None


def _apply_assume_first(
    question: str,
    resolved: ResolvedKeys,
    knowledge: TenantKnowledge,
    deps: AgentDeps,
) -> tuple[str, str | None]:
    """S3: augment the question with default dimensions (flag-gated)."""
    if not deps.config.flags.assume_first:
        return question, None
    keys = tuple(entry.canonical_key for entry in resolved.keys)
    registry = {entry.canonical_key: entry.aggregation for entry in knowledge.keys}
    result: Assumptions = assume_first(
        question=question,
        resolved_keys=keys,
        key_aggregations=registry,
        defaults=dict(deps.config.agent.aggregation_defaults),
        default_window=deps.config.agent.default_time_window,
    )
    return result.question, result.note


def _digest_text(
    conversation: ConversationContext | None,
    digest_cache: DigestCache | None,
    deps: AgentDeps,
) -> str | None:
    """Optional rolling session digest injected into the prompt."""
    if (
        not deps.config.flags.session_digest
        or conversation is None
        or digest_cache is None
        or not conversation.turns
    ):
        return None
    session_id = conversation.turns[-1].session_id
    digest = digest_cache.digest_for(session_id, conversation.turns)
    return digest.text if digest else None
