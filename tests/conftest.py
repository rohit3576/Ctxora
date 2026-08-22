"""Shared test fixtures: deterministic env + knowledge-store state reset."""

import os
from collections.abc import Iterator

import pytest

from knowledge.store import KnowledgeStore

_MANAGED_ENV_PREFIXES = ("METADATA_DB_", "TELEMETRY_DB_", "LLM_", "EMBEDDING_", "FEEDBACK_")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove developer-machine env vars so Settings() is deterministic."""
    for name in list(os.environ):
        if name.startswith(_MANAGED_ENV_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def fresh_knowledge_state() -> Iterator[None]:
    """Reset the knowledge store class state around every test."""
    KnowledgeStore.reset_state()
    yield
    KnowledgeStore.reset_state()
