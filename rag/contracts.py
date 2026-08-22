"""RAG contracts: documents, chunks, retrieval, and the RagStore protocol."""

import datetime
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ChunkInsert:
    """One chunk about to be persisted."""

    page_number: int
    chunk_index: int
    section_title: str
    chunk_text: str
    chunk_hash: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One ingested document."""

    id: str
    tenant: str
    filename: str
    file_hash: str
    total_pages: int
    chunk_count: int
    created_at: datetime.datetime | None = None


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One search hit."""

    document: str
    page_number: int
    section_title: str
    chunk_text: str
    score: float


@runtime_checkable
class RagStore(Protocol):
    """Persistence + vector search for ingested documents."""

    def save_document(
        self,
        tenant: str,
        filename: str,
        file_hash: str,
        chunks: tuple[ChunkInsert, ...],
        total_pages: int,
        embedding_model: str,
        scope: str | None = None,
    ) -> DocumentRecord | None:
        """Persist document + chunks; None when the hash already exists."""
        ...

    def find_by_hash(self, tenant: str, file_hash: str) -> DocumentRecord | None:
        """Look up one document by tenant + content hash."""
        ...

    def list_documents(self, tenant: str) -> list[DocumentRecord]:
        """Active documents for one tenant, newest first."""
        ...

    def delete_document(self, tenant: str, document_id: str) -> bool:
        """Remove one document and its chunks; False when unknown."""
        ...

    def search(
        self,
        query_embedding: list[float],
        tenant: str,
        shared_scope: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Cosine-ranked chunks from tenant docs plus shared-scope docs."""
        ...
