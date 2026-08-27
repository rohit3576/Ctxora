"""Ingestion: parse -> chunk -> embed -> store, with hash dedupe."""

import hashlib
import logging

from config.settings import RagConfig
from llm.client import LLMClient
from rag.chunker import chunk_pages, chunk_pages_v2, parent_max_words
from rag.contracts import PARENT_KIND, ChunkInsert, DocumentRecord, RagStore
from rag.parsers import UnsupportedFormatError, parse, supported

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
) -> DocumentRecord:
    """Ingest one uploaded document; re-upload of the same hash is a no-op.

    Re-uploading with a different chunker version than the stored document
    re-chunks it: the old document is deleted and ingested fresh.

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
        if store.has_parented_chunks(existing.id) == config.chunking_v2:
            _logger.info("document %s already ingested for %s (hash match)", filename, tenant)
            return existing
        _logger.info(
            "document %s stored with a different chunker; re-chunking for %s", filename, tenant
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
    try:
        embeddings = llm.embed([chunk.chunk_text for chunk in embed_rows])
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
    record = store.save_document(
        tenant=tenant,
        filename=filename,
        file_hash=file_hash,
        chunks=embedded,
        total_pages=len(pages),
        embedding_model=embedding_model,
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
