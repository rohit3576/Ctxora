"""Integration: R4f metadata containment filters on live PG + pgvector.

Run explicitly: CTXORA_IT=1 uv run pytest tests/integration
Requires: docker compose up -d (pgvector image).
"""

import os
from collections.abc import Sequence

import psycopg
import pytest

from config.settings import RagConfig, Settings
from database.metadata import conninfo
from knowledge.pg import metadata_query
from llm.client import GenResult
from rag.contracts import DocumentRecord, RagFilters
from rag.ingest import ingest
from rag.store import PGRagStore

pytestmark = pytest.mark.skipif(
    os.environ.get("CTXORA_IT") != "1",
    reason="integration tests run only with CTXORA_IT=1",
)

_TENANT = "rag-filters-it"

_DOC_A = b"""# Manual A

## Range

The acceptable range is 10 to 20 units.
"""

_DOC_B = b"""# Manual B

## Range

The acceptable range is 30 to 40 units.
"""


class _AxisEmbed:
    """Deterministic embedder: 'units alpha' -> axis 0, 'units bravo' -> axis 1."""

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        def vec(axis: int) -> list[float]:
            return [1.0 if i == axis else 0.0 for i in range(1536)]

        return [vec(0) if "alpha" in t.lower() else vec(1) for t in texts]


def _ingest(
    store: PGRagStore, name: str, content: bytes, metadata: dict[str, str]
) -> DocumentRecord:
    return ingest(
        store,
        _AxisEmbed(),
        RagConfig(chunking_v2=True),
        _TENANT,
        name,
        content,
        "test-embed",
        metadata=metadata,
    )


def test_metadata_containment_restricts_search() -> None:
    store = PGRagStore(metadata_query(Settings()))
    try:
        a = _ingest(
            store,
            "alpha.md",
            _DOC_A,
            {"deviceModel": "Sensor-Alpha", "firmwareVersion": "1"},
        )
        b = _ingest(
            store,
            "bravo.md",
            _DOC_B,
            {"deviceModel": "Sensor-Bravo", "firmwareVersion": "2"},
        )
        assert a.metadata == {"deviceModel": "Sensor-Alpha", "firmwareVersion": "1"}
        assert b.status == "ACTIVE"

        query = _AxisEmbed().embed(["units alpha range"])[0]

        unfiltered = store.search(query, _TENANT, "shared", 5)
        assert {hit.document for hit in unfiltered} == {"alpha.md", "bravo.md"}

        only_alpha = store.search(
            query, _TENANT, "shared", 5, RagFilters(device_model="Sensor-Alpha")
        )
        assert only_alpha
        assert all(hit.document == "alpha.md" for hit in only_alpha)

        combined = store.search(
            query,
            _TENANT,
            "shared",
            5,
            RagFilters(device_model="Sensor-Alpha", firmware_version="2"),
        )
        assert combined == []

        free_form = store.search(
            query, _TENANT, "shared", 5, RagFilters(metadata={"deviceModel": "Sensor-Bravo"})
        )
        assert free_form
        assert all(hit.document == "bravo.md" for hit in free_form)
    finally:
        with psycopg.connect(conninfo(Settings()), autocommit=True) as conn:
            conn.execute("DELETE FROM rag_documents WHERE tenant = %s", (_TENANT,))
