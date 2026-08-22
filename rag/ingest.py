"""Ingestion: parse -> chunk -> embed -> store, with hash dedupe."""

import hashlib
import logging

from config.settings import RagConfig
from llm.client import LLMClient
from rag.chunker import chunk_pages
from rag.contracts import ChunkInsert, DocumentRecord, RagStore
from rag.parsers import UnsupportedFormatError, parse, supported

_logger = logging.getLogger("datamind.ingest")


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
        _logger.info("document %s already ingested for %s (hash match)", filename, tenant)
        return existing

    pages = parse(filename, content)
    chunks = chunk_pages(pages, config.chunk_size, config.chunk_overlap)
    if not chunks:
        msg = "document produced no extractable text"
        raise IngestError(msg)

    try:
        embeddings = llm.embed([chunk.chunk_text for chunk in chunks])
    except Exception as exc:
        msg = f"embedding failed: {exc}"
        raise IngestError(msg) from exc

    embedded = tuple(
        ChunkInsert(
            page_number=chunk.page_number,
            chunk_index=chunk.chunk_index,
            section_title=chunk.section_title,
            chunk_text=chunk.chunk_text,
            chunk_hash=chunk.chunk_hash,
            embedding=embedding,
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True)
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
