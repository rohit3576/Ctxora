"""In-memory RagStore fake: exact-neighbor search over cosine similarity."""

import datetime
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import replace
from itertools import chain

from llm.client import GenResult
from rag.contracts import CHILD_KIND, PARENT_KIND, ChunkInsert, DocumentRecord, RetrievedChunk

_EMBED_DIM: int = 1536  # must match rag_chunks.embedding VECTOR(1536)
_TOKEN: re.Pattern[str] = re.compile(r"[a-z0-9][a-z0-9.-]*")


class HashEmbedLLM:
    """Deterministic LLMClient whose embeddings are bag-of-words hashes.

    Cosine over these vectors approximates token overlap (lexical search),
    which makes offline retrieval smoke runs stable and meaningful-shaped.
    Real recall numbers still require a real embedding model.
    """

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        """Canned completion; this fake is only ever used for embedding."""
        return GenResult(sql="SELECT 1", raw="", prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One 1536-dim bag-of-words hash vector per text."""
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * _EMBED_DIM
            for token in _TOKEN.findall(text.lower()):
                digest = hashlib.md5(token.encode("utf-8"), usedforsecurity=False).digest()
                index = int.from_bytes(digest[:4], "big") % _EMBED_DIM
                vector[index] += 1.0
            vectors.append(vector)
        return vectors


class HashScriptedLLM:
    """Hash embeddings + scripted generate() replies, with an embed recorder.

    For rewrite-mechanism tests: deterministic lexical embeddings plus canned
    LLM outputs (a simulated good rewriter). `embedded` records every text
    that reached the embedder — mutation is the documented observation purpose.
    """

    def __init__(self, generated: Sequence[str]) -> None:
        """Queue the canned generate() replies (consumed in order)."""
        self._hash: HashEmbedLLM = HashEmbedLLM()
        self._generated: list[str] = list(generated)
        self.embedded: list[str] = []

    def generate(self, system: str, user: str, *, temperature: float) -> GenResult:
        """Replay the next canned reply."""
        raw = self._generated.pop(0) if self._generated else ""
        return GenResult(sql="", raw=raw, prompt_tokens=1, completion_tokens=1)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Delegate to the hash embedder, recording what was embedded."""
        self.embedded.extend(texts)
        return self._hash.embed(texts)


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


class InMemoryRagStore:
    """Dict-backed store computing cosine scores directly."""

    def __init__(self) -> None:
        """Start empty; documents and chunks keyed by document id."""
        self.documents: dict[str, DocumentRecord] = {}
        self.chunks: dict[str, list[tuple[ChunkInsert, str | None, str]]] = {}
        self._scope_by_document: dict[str, str | None] = {}

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
    ) -> DocumentRecord | None:
        """Persist document + chunks; None when the hash already exists."""
        if self.find_by_hash(tenant, file_hash) is not None:
            return None
        document_id = f"doc-{len(self.documents) + 1}"
        for superseded_id in supersede_ids:
            old = self.documents.get(superseded_id)
            if old is not None and old.tenant == tenant:
                self.documents[superseded_id] = replace(old, status="SUPERSEDED")
        record = DocumentRecord(
            id=document_id,
            tenant=tenant,
            filename=filename,
            file_hash=file_hash,
            total_pages=total_pages,
            chunk_count=len(chunks),
            created_at=datetime.datetime.now(tz=datetime.UTC),
            doc_family=doc_family,
            doc_version=doc_version,
            status=status,
        )
        self.documents[document_id] = record
        self.chunks[document_id] = [(chunk, scope, tenant) for chunk in chunks]
        self._scope_by_document[document_id] = scope
        return record

    def find_by_hash(self, tenant: str, file_hash: str) -> DocumentRecord | None:
        """Look up one document by tenant + content hash."""
        for record in self.documents.values():
            if record.tenant == tenant and record.file_hash == file_hash:
                return record
        return None

    def list_documents(self, tenant: str) -> list[DocumentRecord]:
        """Active documents for one tenant, newest first."""
        records = [
            r for r in self.documents.values() if r.tenant == tenant and r.status == "ACTIVE"
        ]
        records.sort(
            key=lambda r: r.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
            reverse=True,
        )
        return records

    def delete_document(self, tenant: str, document_id: str) -> bool:
        """Remove one document and its chunks; False when unknown."""
        record = self.documents.get(document_id)
        if record is None or record.tenant != tenant:
            return False
        del self.documents[document_id]
        self.chunks.pop(document_id, None)
        self._scope_by_document.pop(document_id, None)
        return True

    def has_parented_chunks(self, document_id: str) -> bool:
        """Whether the document's chunks use the v2 parent/child shape."""
        chunk_list = self.chunks.get(document_id, [])
        return any(chunk.chunk_kind is not None for chunk, _scope, _owner in chunk_list)

    def search(
        self,
        query_embedding: list[float],
        tenant: str,
        shared_scope: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Rank children (and legacy chunks) by cosine; return unique parents.

        Mirrors PGRagStore.search: v2 children rank, their parents return
        (best score per parent); v1 chunks (kind None) pass through.
        """
        hits: dict[tuple[str, str], tuple[float, RetrievedChunk]] = {}
        legacy: list[tuple[float, RetrievedChunk]] = []
        for document_id, chunk_list in self.chunks.items():
            record = self.documents.get(document_id)
            if record is None or record.status != "ACTIVE":
                continue
            if not self._in_scope(document_id, record, tenant, shared_scope):
                continue
            self._rank_document(document_id, record, chunk_list, query_embedding, hits, legacy)
        merged = list(chain(hits.values(), legacy))
        merged.sort(key=lambda pair: pair[0], reverse=True)
        return [chunk for _score, chunk in merged[:top_k]]

    def _in_scope(
        self, document_id: str, record: DocumentRecord, tenant: str, shared_scope: str
    ) -> bool:
        scope = self._scope_by_document.get(document_id)
        return record.tenant == tenant or (scope == shared_scope and record.tenant != tenant)

    def _rank_document(
        self,
        document_id: str,
        record: DocumentRecord,
        chunk_list: list[tuple[ChunkInsert, str | None, str]],
        query_embedding: list[float],
        hits: dict[tuple[str, str], tuple[float, RetrievedChunk]],
        legacy: list[tuple[float, RetrievedChunk]],
    ) -> None:
        parents_by_hash = {
            chunk.chunk_hash: chunk
            for chunk, _scope, _owner in chunk_list
            if chunk.chunk_kind == PARENT_KIND
        }
        for chunk, _scope, _owner in chunk_list:
            if not chunk.embedding:
                continue
            score = _cosine(query_embedding, chunk.embedding)
            if chunk.chunk_kind == CHILD_KIND:
                parent = parents_by_hash.get(chunk.parent_hash or "")
                if parent is None:
                    continue
                key = (document_id, parent.chunk_hash)
                current = hits.get(key)
                if current is None or score > current[0]:
                    hits[key] = (
                        score,
                        RetrievedChunk(
                            document=record.filename,
                            page_number=parent.page_number,
                            section_title=parent.section_title,
                            chunk_text=parent.chunk_text,
                            score=round(score, 4),
                        ),
                    )
            elif chunk.chunk_kind is None:
                legacy.append(
                    (
                        score,
                        RetrievedChunk(
                            document=record.filename,
                            page_number=chunk.page_number,
                            section_title=chunk.section_title,
                            chunk_text=chunk.chunk_text,
                            score=round(score, 4),
                        ),
                    )
                )
