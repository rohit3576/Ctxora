-- Document RAG DDL (PostgreSQL + pgvector). The vector extension is
-- enabled by database.metadata.bootstrap_schema (with the knowledge schema,
-- whose sql_agent_sql_examples.embedding also uses VECTOR).
--
-- R4c (structure-aware chunking + versioned retrieval) adds:
--   rag_documents: doc_family, doc_version, metadata
--   rag_chunks:    parent_id (self-FK), chunk_kind, metadata
-- Everything is additive and idempotent: fresh installs build the full
-- shape via CREATE TABLE IF NOT EXISTS; existing databases catch up via
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS (a no-op on fresh installs).
-- 4d fills parent_id/chunk_kind (children embedded, parents returned);
-- 4e flips obsolete doc_family versions to SUPERSEDED; 4f filters on
-- metadata.

CREATE TABLE IF NOT EXISTS rag_documents (
    id UUID PRIMARY KEY,
    tenant VARCHAR(50) NOT NULL,
    filename VARCHAR(512) NOT NULL,
    file_hash VARCHAR(64) NOT NULL,
    total_pages INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    status VARCHAR(20) DEFAULT 'ACTIVE',
    embedding_model VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    doc_family VARCHAR(100),
    doc_version VARCHAR(50),
    metadata JSONB,
    UNIQUE (tenant, file_hash)
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id UUID PRIMARY KEY,
    document_id UUID REFERENCES rag_documents(id) ON DELETE CASCADE,
    tenant VARCHAR(50),
    scope VARCHAR(50),
    page_number INTEGER,
    chunk_index INTEGER,
    section_title TEXT,
    chunk_text TEXT NOT NULL,
    chunk_hash VARCHAR(64),
    embedding VECTOR(1536),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    parent_id UUID REFERENCES rag_chunks(id) ON DELETE CASCADE,
    chunk_kind VARCHAR(20),
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_document ON rag_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant ON rag_chunks(tenant);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_hnsw ON rag_chunks
    USING hnsw (embedding vector_cosine_ops);

ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS doc_family VARCHAR(100);
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS doc_version VARCHAR(50);
ALTER TABLE rag_documents ADD COLUMN IF NOT EXISTS metadata JSONB;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS parent_id UUID
    REFERENCES rag_chunks(id) ON DELETE CASCADE;
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS chunk_kind VARCHAR(20);
ALTER TABLE rag_chunks ADD COLUMN IF NOT EXISTS metadata JSONB;

CREATE INDEX IF NOT EXISTS idx_rag_chunks_parent ON rag_chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_rag_documents_family ON rag_documents(tenant, doc_family);
