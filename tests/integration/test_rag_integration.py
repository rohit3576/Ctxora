"""Integration: document ingest→retrieve round-trip on live PG + pgvector.

Run explicitly: DATAMIND_IT=1 uv run pytest tests/integration
Requires: docker compose up -d (pgvector image).
"""

import os

import pytest

from config.settings import RagConfig, Settings
from knowledge.pg import metadata_query
from llm.client import GenResult
from rag.ingest import ingest
from rag.rag_flow import retrieve
from rag.store import PGRagStore
from tests.test_rag_core import MANUAL

pytestmark = pytest.mark.skipif(
    os.environ.get("DATAMIND_IT") != "1",
    reason="integration tests run only with DATAMIND_IT=1",
)

_TENANT = "rag-it"


def test_round_trip(tmp_path: object) -> None:
    """Ingest the manual, then retrieve its coolant chunk by cosine search."""
    from collections.abc import Sequence

    class DimEmbed:
        """3-dim deterministic embedder matching the 1536-dim-free fake store."""

        def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
            return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)

        def embed(self, texts: Sequence[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] if "coolant" in t.lower() else [0.0, 1.0, 0.0] for t in texts]

    store = PGRagStore(metadata_query(Settings()))
    record = ingest(
        store,
        DimEmbed(),
        RagConfig(),
        _TENANT,
        "manual.md",
        MANUAL,
        "test-embed",
    )
    assert record.chunk_count > 0

    dedupe = ingest(
        store,
        DimEmbed(),
        RagConfig(),
        _TENANT,
        "renamed.md",
        MANUAL,
        "test-embed",
    )
    assert dedupe.id == record.id

    hits = retrieve(store, DimEmbed(), RagConfig(), _TENANT, "coolant temperature range")
    assert hits
    assert hits[0].document in ("manual.md", "renamed.md")

    assert store.delete_document(_TENANT, record.id) is True
    assert retrieve(store, DimEmbed(), RagConfig(), _TENANT, "coolant") == []
