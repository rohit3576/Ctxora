"""Ingestion: parse -> chunk -> embed -> store, with hash dedupe."""

import hashlib
import logging

from config.settings import RagConfig
from llm.client import LLMClient
from rag.chunker import chunk_pages, chunk_pages_v2, parent_max_words
from rag.contracts import CHILD_KIND, PARENT_KIND, ChunkInsert, DocumentRecord, RagStore
from rag.parsers import UnsupportedFormatError, parse, supported
from rag.versions import version_key

_logger = logging.getLogger("ctxora.ingest")


class IngestError(Exception):
    """Ingestion failed before persistence."""

    def __init__(self, detail: str) -> None:
        """Describe the failure."""
        self.detail: str = detail
        super().__init__(detail)


def ingest(
    store: RagStore,
    llm: LLMClient,
    config: RagConfig,
    tenant: str,
    filename: str,
    content: bytes,
    embedding_model: str,
    doc_family: str | None = None,
    doc_version: str | None = None,
    metadata: dict[str, str] | None = None,
) -> DocumentRecord:
    """Ingest one uploaded document; re-upload of the same hash is a no-op.

    Version lifecycle: when doc_family and doc_version are set, uploading a
    newer version supersedes every older ACTIVE sibling in the same family
    (same transaction as the insert), and uploading an older version than
    an ACTIVE sibling stores the document born SUPERSEDED — obsolete
    manuals can never surface in search.

    Re-uploading with a different chunker version, family/version, or
    metadata than the stored document re-chunks it.

    Raises:
        UnsupportedFormatError: filename suffix has no parser.
        IngestError: empty parse, oversized upload, or embedding failure.
    """
    if not supported("." + filename.rsplit(".", 1)[-1] if "." in filename else ""):
        raise UnsupportedFormatError(filename.rsplit(".", 1)[-1] if "." in filename else filename)
    if len(content) > config.max_upload_mb * 1024 * 1024:
        msg = f"upload exceeds {config.max_upload_mb} MB limit"
        raise IngestError(msg)

    file_hash = hashlib.sha256(content).hexdigest()
    existing = store.find_by_hash(tenant, file_hash)
    if existing is not None:
        same_chunker = store.has_parented_chunks(existing.id) == config.chunking_v2
        same_meta = (
            existing.doc_family == doc_family
            and existing.doc_version == doc_version
            and existing.metadata == (dict(metadata) if metadata else None)
        )
        if same_chunker and same_meta:
            _logger.info("document %s already ingested for %s (hash match)", filename, tenant)
            return existing
        _logger.info(
            "document %s stored with different chunker or metadata; re-chunking for %s",
            filename,
            tenant,
        )
        store.delete_document(tenant, existing.id)

    pages = parse(filename, content)
    if config.chunking_v2:
        chunks = chunk_pages_v2(pages, config.chunk_size, parent_max_words(config.chunk_size))
    else:
        chunks = chunk_pages(pages, config.chunk_size, config.chunk_overlap)
    if not chunks:
        msg = "document produced no extractable text"
        raise IngestError(msg)

    embed_rows = [chunk for chunk in chunks if chunk.chunk_kind != PARENT_KIND]
    embed_inputs = [
        (
            f"{chunk.section_title}\n\n{chunk.chunk_text}"
            if chunk.chunk_kind == CHILD_KIND
            else chunk.chunk_text
        )
        for chunk in embed_rows
    ]
    try:
        embeddings = llm.embed(embed_inputs)
    except Exception as exc:
        msg = f"embedding failed: {exc}"
        raise IngestError(msg) from exc

    embedding_iter = iter(embeddings)
    embedded = tuple(
        ChunkInsert(
            page_number=chunk.page_number,
            chunk_index=chunk.chunk_index,
            section_title=chunk.section_title,
            chunk_text=chunk.chunk_text,
            chunk_hash=chunk.chunk_hash,
            embedding=[] if chunk.chunk_kind == PARENT_KIND else next(embedding_iter),
            chunk_kind=chunk.chunk_kind,
            parent_hash=chunk.parent_hash,
        )
        for chunk in chunks
    )
    new_key = version_key(doc_version) if doc_version else None
    siblings = (
        [d for d in store.list_documents(tenant) if d.doc_family == doc_family]
        if doc_family
        else []
    )
    supersede_ids = tuple(
        d.id
        for d in siblings
        if new_key is not None and d.doc_version and version_key(d.doc_version) < new_key
    )
    status = (
        "SUPERSEDED"
        if new_key is not None
        and any(d.doc_version and version_key(d.doc_version) > new_key for d in siblings)
        else "ACTIVE"
    )
    if supersede_ids:
        _logger.info(
            "superseding %s older %s document(s) with version %s",
            len(supersede_ids),
            doc_family,
            doc_version,
        )
    record = store.save_document(
        tenant=tenant,
        filename=filename,
        file_hash=file_hash,
        chunks=embedded,
        total_pages=len(pages),
        embedding_model=embedding_model,
        doc_family=doc_family,
        doc_version=doc_version,
        supersede_ids=supersede_ids,
        status=status,
        metadata=metadata,
    )
    if record is None:
        msg = "document disappeared during save"
        raise IngestError(msg)
    _logger.info(
        "ingested %s for %s: %s chunks across %s pages",
        filename,
        tenant,
        record.chunk_count,
        record.total_pages,
    )
    return record
