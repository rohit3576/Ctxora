"""Integration: R4e version lifecycle on live PG — supersede + ACTIVE-only search.

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
from rag.contracts import DocumentRecord
from rag.ingest import ingest
from rag.store import PGRagStore

pytestmark = pytest.mark.skipif(
    os.environ.get("CTXORA_IT") != "1",
    reason="integration tests run only with CTXORA_IT=1",
)

_TENANT = "rag-version-it"
_FAMILY = "test-family-manual"

_V1 = b"""# Test Manual

## Error Codes

| Code | Meaning |
| --- | --- |
| E-901 | Old meaning alpha |
| E-902 | Shared meaning beta |

## Notes

The old revision sets the interval to 10 s.
"""

_V2 = b"""# Test Manual

## Error Codes

| Code | Meaning |
| --- | --- |
| E-901 | New meaning gamma |
| E-902 | Shared meaning beta |

## Notes

The new revision sets the interval to 20 s.
"""


class _AxisEmbed:
    """Deterministic axis-unit embedder: 'gamma' -> axis 0, else axis 1."""

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        return GenResult(sql="", raw="ok", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        def vec(axis: int) -> list[float]:
            return [1.0 if i == axis else 0.0 for i in range(1536)]

        return [vec(0) if "gamma" in t.lower() else vec(1) for t in texts]


def _ingest(store: PGRagStore, name: str, content: bytes, version: str) -> DocumentRecord:
    return ingest(
        store,
        _AxisEmbed(),
        RagConfig(chunking_v2=True),
        _TENANT,
        name,
        content,
        "test-embed",
        doc_family=_FAMILY,
        doc_version=version,
    )


def _cleanup(store: PGRagStore, tenant: str) -> None:
    """Delete every tenant document, SUPERSEDED ones included (cascade)."""
    for record in store.list_documents(tenant):
        store.delete_document(tenant, record.id)
    with psycopg.connect(conninfo(Settings()), autocommit=True) as conn:
        conn.execute("DELETE FROM rag_documents WHERE tenant = %s", (tenant,))


def test_newer_version_supersedes_and_only_active_answers() -> None:
    store = PGRagStore(metadata_query(Settings()))
    try:
        v1 = _ingest(store, "manual-v1.md", _V1, "1")
        assert v1.status == "ACTIVE"

        v2 = _ingest(store, "manual-v2.md", _V2, "2")
        assert v2.status == "ACTIVE"

        listed = store.list_documents(_TENANT)
        assert [r.id for r in listed] == [v2.id]

        old = store.find_by_hash(_TENANT, v1.file_hash)
        assert old is not None and old.status == "SUPERSEDED"
        assert old.doc_family == _FAMILY and old.doc_version == "1"

        hits = store.search(_AxisEmbed().embed(["gamma meaning"])[0], _TENANT, "shared", 5)
        assert hits, "search must answer from the active revision"
        assert all(h.document == "manual-v2.md" for h in hits)
        assert "New meaning gamma" in hits[0].chunk_text

        stale = _ingest(store, "manual-v1-copy.md", _V1, "1")
        assert stale.status == "SUPERSEDED", "older upload is born superseded"
        listed = store.list_documents(_TENANT)
        assert [r.id for r in listed] == [v2.id]
    finally:
        _cleanup(store, _TENANT)
