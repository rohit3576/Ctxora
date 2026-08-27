"""RAG contracts: documents, chunks, retrieval, filters, and the RagStore protocol."""

import datetime
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

PARENT_KIND = "PARENT"
CHILD_KIND = "CHILD"
DEVICE_MODEL_KEY = "deviceModel"
FIRMWARE_VERSION_KEY = "firmwareVersion"


@dataclass(frozen=True, slots=True)
class RagFilters:
    """Deterministic retrieval filters: metadata containment on documents.

    deviceModel and firmwareVersion are sugar for the same-named metadata
    keys; `metadata` adds free-form key/value constraints. A document
    matches only when its metadata contains every constraint.
    """

    device_model: str | None = None
    firmware_version: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def containment(self) -> dict[str, str]:
        """Fold every constraint into one flat key/value map."""
        folded = dict(self.metadata)
        if self.device_model is not None:
            folded[DEVICE_MODEL_KEY] = self.device_model
        if self.firmware_version is not None:
            folded[FIRMWARE_VERSION_KEY] = self.firmware_version
        return folded


@dataclass(frozen=True, slots=True)
class ChunkInsert:
    """One chunk about to be persisted.

    v2 chunking emits parents (whole bounded sections, chunk_kind=PARENT,
    never embedded) and children (structural units, chunk_kind=CHILD,
    parent_hash links to the parent's chunk_hash). v1 chunks leave both
    fields None.
    """

    page_number: int
    chunk_index: int
    section_title: str
    chunk_text: str
    chunk_hash: str
    embedding: list[float]
    chunk_kind: str | None = None
    parent_hash: str | None = None


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
    doc_family: str | None = None
    doc_version: str | None = None
    status: str = "ACTIVE"
    metadata: dict[str, str] | None = None


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
        doc_family: str | None = None,
        doc_version: str | None = None,
        supersede_ids: tuple[str, ...] = (),
        status: str = "ACTIVE",
        metadata: dict[str, str] | None = None,
    ) -> DocumentRecord | None:
        """Persist document + chunks; None when the hash already exists.

        supersede_ids flip to SUPERSEDED in the same transaction as the
        document insert; status lets an already-obsolete upload be born
        SUPERSEDED; metadata is the flat string map filters match against.
        """
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

    def has_parented_chunks(self, document_id: str) -> bool:
        """Whether the document's chunks use the v2 parent/child shape."""
        ...

    def search(
        self,
        query_embedding: list[float],
        tenant: str,
        shared_scope: str,
        top_k: int,
        filters: RagFilters | None = None,
    ) -> list[RetrievedChunk]:
        """Cosine-ranked chunks from tenant docs plus shared-scope docs.

        filters, when set and non-empty, restrict results to documents
        whose metadata contains every constraint (wrong manuals become
        deterministically unreachable).
        """
        ...
