-- Feedback flywheel DDL (PostgreSQL).
-- Status lifecycle: pending | auto_pending | approved | rejected | review.
-- Approved pairs are promoted into sql_agent_sql_examples (Phase 1 schema
-- already carries embedding/status/provenance/usage columns there).

CREATE TABLE IF NOT EXISTS query_feedback (
    id SERIAL PRIMARY KEY,
    tenant VARCHAR(50) NOT NULL,
    session_id VARCHAR(64),
    history_id BIGINT REFERENCES llm_sql_history(id) ON DELETE SET NULL,
    nl_query TEXT NOT NULL,
    generated_sql TEXT,
    feedback_type VARCHAR(10) NOT NULL,
    user_comment TEXT,
    corrected_sql TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    correction_delta JSONB
);

CREATE INDEX IF NOT EXISTS idx_query_feedback_status ON query_feedback(status);
CREATE INDEX IF NOT EXISTS idx_query_feedback_tenant ON query_feedback(tenant);

-- S3 (structural flywheel): labeled correction deltas, additive.
ALTER TABLE query_feedback ADD COLUMN IF NOT EXISTS correction_delta JSONB;
