"""PostgreSQL RagStore: pgvector storage with cosine search."""

import datetime
import uuid

from knowledge.store import Query
from rag.contracts import ChunkInsert, DocumentRecord, RetrievedChunk


def _text(value: object) -> str:
    """Narrow a cell to str; UUID cells stringify (psycopg returns UUID objects)."""
    if isinstance(value, str):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    return ""


def _int(value: object) -> int:
    """Narrow a cell to int (0 when absent)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _vector_literal(embedding: list[float]) -> str:
    """Pgvector literal for one embedding."""
    return "[" + ",".join(str(value) for value in embedding) + "]"


class PGRagStore:
    """rag_documents + rag_chunks in PostgreSQL with pgvector."""

    def __init__(self, query: Query) -> None:
        """Bind the (sql, params) -> rows executor (commits writes)."""
        self._query: Query = query

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
        if self.find_by_hash(tenant, file_hash) is not None:
            return None
        document_id = str(uuid.uuid4())
        created = datetime.datetime.now(tz=datetime.UTC)
        self._query(
            "INSERT INTO rag_documents "
            "(id, tenant, filename, file_hash, total_pages, chunk_count, "
            "status, embedding_model, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE',%s,%s)",
            (
                document_id,
                tenant,
                filename,
                file_hash,
                total_pages,
                len(chunks),
                embedding_model,
                created,
            ),
        )
        for chunk in chunks:
            chunk_id = str(uuid.uuid4())
            self._query(
                "INSERT INTO rag_chunks "
                "(id, document_id, tenant, scope, page_number, chunk_index, "
                "section_title, chunk_text, chunk_hash, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                (
                    chunk_id,
                    document_id,
                    tenant,
                    scope,
                    chunk.page_number,
                    chunk.chunk_index,
                    chunk.section_title,
                    chunk.chunk_text,
                    chunk.chunk_hash,
                    _vector_literal(chunk.embedding),
                ),
            )
        return DocumentRecord(
            id=document_id,
            tenant=tenant,
            filename=filename,
            file_hash=file_hash,
            total_pages=total_pages,
            chunk_count=len(chunks),
            created_at=created,
        )

    def find_by_hash(self, tenant: str, file_hash: str) -> DocumentRecord | None:
        """Look up one document by tenant + content hash."""
        rows = self._query(
            "SELECT id, tenant, filename, file_hash, total_pages, chunk_count, created_at "
            "FROM rag_documents WHERE tenant = %s AND file_hash = %s",
            (tenant, file_hash),
        )
        return self._record(rows[0]) if rows else None

    def list_documents(self, tenant: str) -> list[DocumentRecord]:
        """Active documents for one tenant, newest first."""
        rows = self._query(
            "SELECT id, tenant, filename, file_hash, total_pages, chunk_count, created_at "
            "FROM rag_documents WHERE tenant = %s AND status = 'ACTIVE' "
            "ORDER BY created_at DESC",
            (tenant,),
        )
        return [self._record(row) for row in rows]

    def delete_document(self, tenant: str, document_id: str) -> bool:
        """Remove one document and its chunks (cascade); False when unknown."""
        rows = self._query(
            "DELETE FROM rag_documents WHERE id = %s AND tenant = %s RETURNING id",
            (document_id, tenant),
        )
        return bool(rows)

    def search(
        self,
        query_embedding: list[float],
        tenant: str,
        shared_scope: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Cosine-ranked chunks from tenant docs plus shared-scope docs."""
        rows = self._query(
            "SELECT d.filename, c.page_number, c.section_title, c.chunk_text, "
            "1 - (c.embedding <=> %s::vector) AS score "
            "FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id "
            "WHERE (c.tenant = %s OR (c.tenant IS DISTINCT FROM %s AND c.scope = %s)) "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s",
            (
                _vector_literal(query_embedding),
                tenant,
                tenant,
                shared_scope,
                _vector_literal(query_embedding),
                top_k,
            ),
        )
        return [
            RetrievedChunk(
                document=_text(row[0]),
                page_number=_int(row[1]),
                section_title=_text(row[2]),
                chunk_text=_text(row[3]),
                score=round(float(row[4]), 4) if isinstance(row[4], (int, float)) else 0.0,
            )
            for row in rows
        ]

    def _record(self, row: tuple[object, ...]) -> DocumentRecord:
        """Shape one SELECT row into a DocumentRecord."""
        return DocumentRecord(
            id=_text(row[0]),
            tenant=_text(row[1]),
            filename=_text(row[2]),
            file_hash=_text(row[3]),
            total_pages=_int(row[4]),
            chunk_count=_int(row[5]),
            created_at=row[6] if isinstance(row[6], datetime.datetime) else None,
        )
