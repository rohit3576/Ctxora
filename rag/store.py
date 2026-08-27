"""PostgreSQL RagStore: pgvector storage with cosine search."""

import datetime
import json
import uuid
from itertools import chain

from pydantic import TypeAdapter, ValidationError

from knowledge.store import Query
from rag.contracts import (
    CHILD_KIND,
    PARENT_KIND,
    ChunkInsert,
    DocumentRecord,
    RagFilters,
    RetrievedChunk,
)

_OVERSAMPLE_FACTOR = 3
_METADATA_ADAPTER = TypeAdapter(dict[str, str])

_CHILD_SEARCH_SQL = (
    "SELECT p.id, d.filename, p.page_number, p.section_title, p.chunk_text, "
    "MAX(1 - (c.embedding <=> %s::vector)) AS score "
    "FROM rag_chunks c "
    "JOIN rag_chunks p ON p.id = c.parent_id "
    "JOIN rag_documents d ON d.id = p.document_id "
    "WHERE (c.tenant = %s OR (c.tenant IS DISTINCT FROM %s AND c.scope = %s)) "
    "AND c.chunk_kind = %s AND d.status = 'ACTIVE' "
    "GROUP BY p.id, d.filename, p.page_number, p.section_title, p.chunk_text "
    "ORDER BY score DESC LIMIT %s"
)

_LEGACY_SEARCH_SQL = (
    "SELECT NULL, d.filename, c.page_number, c.section_title, c.chunk_text, "
    "1 - (c.embedding <=> %s::vector) AS score "
    "FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id "
    "WHERE (c.tenant = %s OR (c.tenant IS DISTINCT FROM %s AND c.scope = %s)) "
    "AND c.chunk_kind IS NULL AND d.status = 'ACTIVE' "
    "ORDER BY c.embedding <=> %s::vector LIMIT %s"
)

_CHILD_SEARCH_FILTERED_SQL = (
    "SELECT p.id, d.filename, p.page_number, p.section_title, p.chunk_text, "
    "MAX(1 - (c.embedding <=> %s::vector)) AS score "
    "FROM rag_chunks c "
    "JOIN rag_chunks p ON p.id = c.parent_id "
    "JOIN rag_documents d ON d.id = p.document_id "
    "WHERE (c.tenant = %s OR (c.tenant IS DISTINCT FROM %s AND c.scope = %s)) "
    "AND c.chunk_kind = %s AND d.status = 'ACTIVE' AND d.metadata @> %s::jsonb "
    "GROUP BY p.id, d.filename, p.page_number, p.section_title, p.chunk_text "
    "ORDER BY score DESC LIMIT %s"
)

_LEGACY_SEARCH_FILTERED_SQL = (
    "SELECT NULL, d.filename, c.page_number, c.section_title, c.chunk_text, "
    "1 - (c.embedding <=> %s::vector) AS score "
    "FROM rag_chunks c JOIN rag_documents d ON d.id = c.document_id "
    "WHERE (c.tenant = %s OR (c.tenant IS DISTINCT FROM %s AND c.scope = %s)) "
    "AND c.chunk_kind IS NULL AND d.status = 'ACTIVE' AND d.metadata @> %s::jsonb "
    "ORDER BY c.embedding <=> %s::vector LIMIT %s"
)


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


def _metadata(value: object) -> dict[str, str] | None:
    """Narrow a JSONB cell to a flat string map (None when not one)."""
    try:
        return _METADATA_ADAPTER.validate_python(value)
    except ValidationError:
        return None


_FIND_BY_HASH_SQL = (
    "SELECT id, tenant, filename, file_hash, total_pages, chunk_count, "
    "created_at, doc_family, doc_version, status, metadata "
    "FROM rag_documents WHERE tenant = %s AND file_hash = %s"
)

_LIST_DOCUMENTS_SQL = (
    "SELECT id, tenant, filename, file_hash, total_pages, chunk_count, "
    "created_at, doc_family, doc_version, status, metadata "
    "FROM rag_documents WHERE tenant = %s AND status = 'ACTIVE' "
    "ORDER BY created_at DESC"
)


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
        doc_family: str | None = None,
        doc_version: str | None = None,
        supersede_ids: tuple[str, ...] = (),
        status: str = "ACTIVE",
        metadata: dict[str, str] | None = None,
    ) -> DocumentRecord | None:
        """Persist document + chunks; None when the hash already exists."""
        if self.find_by_hash(tenant, file_hash) is not None:
            return None
        document_id = str(uuid.uuid4())
        created = datetime.datetime.now(tz=datetime.UTC)
        insert_values = (
            document_id,
            tenant,
            filename,
            file_hash,
            total_pages,
            len(chunks),
            status,
            embedding_model,
            doc_family,
            doc_version,
            json.dumps(metadata) if metadata else None,
            created,
        )
        if supersede_ids:
            self._query(
                "WITH supersede AS ("
                "UPDATE rag_documents SET status = 'SUPERSEDED' "
                "WHERE tenant = %s AND id = ANY(%s) RETURNING id) "
                "INSERT INTO rag_documents "
                "(id, tenant, filename, file_hash, total_pages, chunk_count, "
                "status, embedding_model, doc_family, doc_version, metadata, created_at) "
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s"
                " FROM (SELECT count(*) FROM supersede) AS one",
                (tenant, list(supersede_ids), *insert_values),
            )
        else:
            self._query(
                "INSERT INTO rag_documents "
                "(id, tenant, filename, file_hash, total_pages, chunk_count, "
                "status, embedding_model, doc_family, doc_version, metadata, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                insert_values,
            )
        parents = [chunk for chunk in chunks if chunk.chunk_kind == PARENT_KIND]
        children = [chunk for chunk in chunks if chunk.chunk_kind == CHILD_KIND]
        parent_ids = {chunk.chunk_hash: str(uuid.uuid4()) for chunk in parents}
        for chunk in parents:
            self._insert_chunk(
                parent_ids[chunk.chunk_hash], document_id, tenant, scope, chunk, parent_id=None
            )
        for chunk in children:
            if chunk.parent_hash not in parent_ids:
                msg = f"child references unknown parent {chunk.parent_hash!r}"
                raise ValueError(msg)
            self._insert_chunk(
                None, document_id, tenant, scope, chunk, parent_id=parent_ids[chunk.parent_hash]
            )
        for chunk in chunks:
            if chunk.chunk_kind is None:
                self._insert_chunk(None, document_id, tenant, scope, chunk, parent_id=None)
        return DocumentRecord(
            id=document_id,
            tenant=tenant,
            filename=filename,
            file_hash=file_hash,
            total_pages=total_pages,
            chunk_count=len(chunks),
            created_at=created,
            doc_family=doc_family,
            doc_version=doc_version,
            status=status,
            metadata=dict(metadata) if metadata else None,
        )

    def _insert_chunk(
        self,
        chunk_id: str | None,
        document_id: str,
        tenant: str,
        scope: str | None,
        chunk: ChunkInsert,
        parent_id: str | None,
    ) -> None:
        row_id = chunk_id or str(uuid.uuid4())
        embedding = _vector_literal(chunk.embedding) if chunk.embedding else None
        self._query(
            "INSERT INTO rag_chunks "
            "(id, document_id, tenant, scope, page_number, chunk_index, "
            "section_title, chunk_text, chunk_hash, embedding, parent_id, chunk_kind) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)",
            (
                row_id,
                document_id,
                tenant,
                scope,
                chunk.page_number,
                chunk.chunk_index,
                chunk.section_title,
                chunk.chunk_text,
                chunk.chunk_hash,
                embedding,
                parent_id,
                chunk.chunk_kind,
            ),
        )

    def find_by_hash(self, tenant: str, file_hash: str) -> DocumentRecord | None:
        """Look up one document by tenant + content hash."""
        rows = self._query(_FIND_BY_HASH_SQL, (tenant, file_hash))
        return self._record(rows[0]) if rows else None

    def list_documents(self, tenant: str) -> list[DocumentRecord]:
        """Active documents for one tenant, newest first."""
        rows = self._query(_LIST_DOCUMENTS_SQL, (tenant,))
        return [self._record(row) for row in rows]

    def delete_document(self, tenant: str, document_id: str) -> bool:
        """Remove one document and its chunks (cascade); False when unknown."""
        rows = self._query(
            "DELETE FROM rag_documents WHERE id = %s AND tenant = %s RETURNING id",
            (document_id, tenant),
        )
        return bool(rows)

    def has_parented_chunks(self, document_id: str) -> bool:
        """Whether the document's chunks use the v2 parent/child shape."""
        rows = self._query(
            "SELECT 1 FROM rag_chunks WHERE document_id = %s AND chunk_kind IS NOT NULL LIMIT 1",
            (document_id,),
        )
        return bool(rows)

    def search(
        self,
        query_embedding: list[float],
        tenant: str,
        shared_scope: str,
        top_k: int,
        filters: RagFilters | None = None,
    ) -> list[RetrievedChunk]:
        """Rank children (and legacy chunks) by cosine; return unique parents.

        v2 documents: CHILD rows are ranked, their PARENT rows returned
        (best score per parent, oversampled 3x then deduped). v1 documents
        (chunk_kind NULL) rank directly and pass through unchanged.
        filters restrict results to documents whose metadata contains
        every constraint.
        """
        vector = _vector_literal(query_embedding)
        scope_params = (tenant, tenant, shared_scope)
        containment = filters.containment() if filters is not None else {}
        if containment:
            containment_json = json.dumps(containment)
            child_rows = self._query(
                _CHILD_SEARCH_FILTERED_SQL,
                (
                    vector,
                    *scope_params,
                    CHILD_KIND,
                    containment_json,
                    top_k * _OVERSAMPLE_FACTOR,
                ),
            )
            legacy_rows = self._query(
                _LEGACY_SEARCH_FILTERED_SQL,
                (vector, *scope_params, containment_json, vector, top_k),
            )
        else:
            child_rows = self._query(
                _CHILD_SEARCH_SQL,
                (vector, *scope_params, CHILD_KIND, top_k * _OVERSAMPLE_FACTOR),
            )
            legacy_rows = self._query(_LEGACY_SEARCH_SQL, (vector, *scope_params, vector, top_k))
        merged = [
            (
                _text(row[0]),
                _text(row[1]),
                _int(row[2]),
                _text(row[3]),
                _text(row[4]),
                round(float(row[5]), 4) if isinstance(row[5], (int, float)) else 0.0,
            )
            for row in chain(child_rows, legacy_rows)
        ]
        seen: set[tuple[str, str, int, str, str]] = set()
        unique: list[tuple[str, str, int, str, str, float]] = []
        for parent_id, filename, page, title, text, score in merged:
            key = (parent_id, filename, page, title, text)
            if key in seen:
                continue
            seen.add(key)
            unique.append((parent_id, filename, page, title, text, score))
        unique.sort(key=lambda row: row[5], reverse=True)
        return [
            RetrievedChunk(
                document=row[1],
                page_number=row[2],
                section_title=row[3],
                chunk_text=row[4],
                score=row[5],
            )
            for row in unique[:top_k]
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
            doc_family=_text(row[7]) or None,
            doc_version=_text(row[8]) or None,
            status=_text(row[9]) or "ACTIVE",
            metadata=_metadata(row[10]),
        )
