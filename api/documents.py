"""Documents API: upload (multipart), list, delete."""

import logging
from collections.abc import Callable
from typing import ClassVar

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from api.schemas import Envelope
from config.settings import RagConfig
from llm.client import LLMClient
from rag.contracts import DocumentRecord, RagStore
from rag.ingest import IngestError, ingest
from rag.parsers import UnsupportedFormatError

_logger = logging.getLogger("querypulse.documents")

IngestFn = Callable[[str, str, bytes], DocumentRecord]


class DocumentView(BaseModel):
    """One ingested document on the wire."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    documentId: str
    filename: str
    totalPages: int
    chunkCount: int


def _failure(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Render one Failure envelope."""
    body = Envelope[None](status="Failure", message=message, data=None)
    content: dict[str, object] = body.model_dump()
    content["errorType"] = error_type
    content["statusCode"] = status_code
    return JSONResponse(status_code=status_code, content=content)


def build_ingest_fn(
    rag_store: RagStore, llm: LLMClient, config: RagConfig, embedding_model: str
) -> IngestFn:
    """Bind the ingest dependencies into one callable."""

    def ingest_for(tenant: str, filename: str, content: bytes) -> DocumentRecord:
        """Run ingestion for one upload."""
        return ingest(rag_store, llm, config, tenant, filename, content, embedding_model)

    return ingest_for


def build_documents_router(rag_store: RagStore, ingest_fn: IngestFn) -> APIRouter:
    """Build the document management router with deps closed over."""

    def upload(tenant: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
        """Parse, chunk, embed, and store one document."""
        filename = file.filename or "upload"
        content = file.file.read()
        try:
            record = ingest_fn(tenant, filename, content)
        except UnsupportedFormatError as exc:
            return _failure(415, "UNSUPPORTED_FORMAT", str(exc))
        except IngestError as exc:
            return _failure(400, "INGEST_FAILED", exc.detail)
        data = DocumentView(
            documentId=record.id,
            filename=record.filename,
            totalPages=record.total_pages,
            chunkCount=record.chunk_count,
        )
        envelope = Envelope[DocumentView](status="Success", message="document ingested", data=data)
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    def list_docs(tenant: str) -> JSONResponse:
        """List the tenant's active documents."""
        views = [
            DocumentView(
                documentId=record.id,
                filename=record.filename,
                totalPages=record.total_pages,
                chunkCount=record.chunk_count,
            )
            for record in rag_store.list_documents(tenant)
        ]
        envelope = Envelope[list[DocumentView]](
            status="Success", message="documents listed", data=views
        )
        content: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=content)

    def delete(tenant: str, document_id: str) -> JSONResponse:
        """Delete one document and its chunks."""
        if not rag_store.delete_document(tenant, document_id):
            return _failure(404, "NOT_FOUND", "document not found")
        envelope = Envelope[dict[str, bool]](
            status="Success", message="document deleted", data={"deleted": True}
        )
        ok: dict[str, object] = envelope.model_dump()
        return JSONResponse(status_code=200, content=ok)

    router = APIRouter(tags=["documents"])
    router.add_api_route("/v1/documents", upload, methods=["POST"])
    router.add_api_route("/v1/documents", list_docs, methods=["GET"])
    router.add_api_route("/v1/documents/{tenant}/{document_id}", delete, methods=["DELETE"])
    return router
