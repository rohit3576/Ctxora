"""Ctxora application entry point.

App factory pattern: tests construct isolated apps with overridden
dependencies (store, knowledge fetcher, LLM, memory); gunicorn binds
``main:app``.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI

from agent.pipeline import AgentDeps
from api.auth import TenantAuth
from api.documents import build_documents_router, build_ingest_fn
from api.feedback import build_feedback_router
from api.feedback_admin import build_feedback_admin_router
from api.health import build_health_router
from api.history import build_history_router
from api.onboarding import build_onboarding_router
from api.query import build_query_router
from api.rag import build_rag_router
from api.ratelimit import TokenBucketLimiter
from api.stream import build_stream_router
from config.settings import (
    DEFAULT_CONFIG_PATH,
    Settings,
    get_settings,
    load_app_config,
)
from database import metadata
from database.contracts import TelemetryStore
from database.factory import build_telemetry_store
from feedback.contracts import FeedbackStore
from feedback.store import PGFeedbackStore
from knowledge.pg import metadata_query
from knowledge.store import KnowledgeStore, Query
from llm.client import LLMClient
from llm.openai_compat import OpenAICompatibleClient
from memory.contracts import MemoryStore
from memory.pg import PGMemoryStore
from onboarding.wizard import tenant_disabled
from rag.contracts import RagStore
from rag.store import PGRagStore

if TYPE_CHECKING:
    from collections.abc import Callable

_DEFAULT_PING_INTERVAL_S = 15.0


def create_app(
    settings: Settings | None = None,
    config_path: Path | None = None,
    *,
    store: TelemetryStore | None = None,
    knowledge_query: Query | None = None,
    llm: LLMClient | None = None,
    memory: MemoryStore | None = None,
    feedback: FeedbackStore | None = None,
    rag_store: RagStore | None = None,
    stream_ping_interval_s: float = _DEFAULT_PING_INTERVAL_S,
) -> FastAPI:
    """Build the application; every dependency is injectable for tests."""
    resolved_settings = settings if settings is not None else get_settings()
    app_config = load_app_config(config_path or DEFAULT_CONFIG_PATH)  # fail fast at boot

    resolved_store = (
        store if store is not None else build_telemetry_store(app_config, resolved_settings)
    )
    executor: Callable[[str, tuple[object, ...]], list[tuple[object, ...]]] = (
        knowledge_query if knowledge_query is not None else metadata_query(resolved_settings)
    )
    knowledge = KnowledgeStore(query=executor)
    resolved_llm = llm if llm is not None else OpenAICompatibleClient(resolved_settings)
    resolved_memory = (
        memory if memory is not None else PGMemoryStore(metadata_query(resolved_settings))
    )
    resolved_feedback = feedback if feedback is not None else PGFeedbackStore(executor)
    resolved_rag = rag_store if rag_store is not None else PGRagStore(executor)

    deps = AgentDeps(store=resolved_store, knowledge=knowledge, llm=resolved_llm, config=app_config)

    app = FastAPI(title="Ctxora", version="0.2.0")
    app.state.config = app_config
    app.include_router(build_health_router(resolved_settings, metadata.check_metadata_db))
    app.include_router(
        build_query_router(
            deps,
            resolved_memory,
            resolved_feedback,
            resolved_rag,
            auth=TenantAuth(resolved_settings),
            limiter=TokenBucketLimiter(app_config.ratelimit),
            active_check=lambda tenant: not tenant_disabled(executor, tenant),
        )
    )
    app.include_router(build_rag_router(resolved_rag, resolved_llm, app_config.rag))
    app.include_router(
        build_documents_router(
            resolved_rag,
            build_ingest_fn(
                resolved_rag, resolved_llm, app_config.rag, resolved_settings.embedding_model
            ),
        )
    )
    app.include_router(build_history_router(resolved_memory))
    app.include_router(build_onboarding_router(deps, executor))
    if app_config.flags.feedback_capture:
        app.include_router(build_feedback_router(resolved_feedback, resolved_memory))
        app.include_router(
            build_feedback_admin_router(
                resolved_feedback,
                resolved_llm,
                resolved_settings.feedback_admin_token,
                resolved_settings.embedding_model,
            )
        )
    if app_config.flags.streaming:
        app.include_router(
            build_stream_router(deps, resolved_memory, ping_interval_s=stream_ping_interval_s)
        )
    return app


app = create_app()
