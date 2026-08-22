"""Conversation memory DDL (PostgreSQL).

supersedes_id ships now (Phase 2) and is consumed by the correction loop
in Phase 3.
"""

CREATE TABLE IF NOT EXISTS llm_sessions (
    id UUID PRIMARY KEY,
    tenant VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    user_email VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_sessions_tenant ON llm_sessions(tenant);

CREATE TABLE IF NOT EXISTS llm_sql_history (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID REFERENCES llm_sessions(id) ON DELETE CASCADE,
    tenant VARCHAR(50) NOT NULL,
    nl_query TEXT NOT NULL,
    sql TEXT,
    data JSONB,
    summary TEXT,
    token_usage INTEGER DEFAULT 0,
    supersedes_id BIGINT REFERENCES llm_sql_history(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_sql_history_session ON llm_sql_history(session_id);
CREATE INDEX IF NOT EXISTS idx_llm_sql_history_tenant ON llm_sql_history(tenant);
