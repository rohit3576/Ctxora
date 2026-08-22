-- Document RAG DDL (PostgreSQL + pgvector). The vector extension is
-- enabled by database.metadata.bootstrap_schema (with the knowledge schema,
-- whose sql_agent_sql_examples.embedding also uses VECTOR).

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
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_document ON rag_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_tenant ON rag_chunks(tenant);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_hnsw ON rag_chunks
    USING hnsw (embedding vector_cosine_ops);
