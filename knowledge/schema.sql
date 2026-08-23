-- Ctxora knowledge store DDL (PostgreSQL).
-- The SQL knowledge base lives entirely in these tables; no markdown files.

CREATE TABLE IF NOT EXISTS sql_agent_tenants (
    id                 SERIAL PRIMARY KEY,
    tenant_name        VARCHAR(50) UNIQUE NOT NULL,
    display_name       VARCHAR(100),
    status             VARCHAR(20) DEFAULT 'active',
    eav_rules_text     TEXT,
    onboarding_answers JSONB,
    created_at         TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sql_agent_telemetry_registry (
    id                  SERIAL PRIMARY KEY,
    tenant_id           INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    canonical_key       VARCHAR(100) NOT NULL,
    physical_key        VARCHAR(100),
    description         TEXT,
    datatype            VARCHAR(50),
    unit                VARCHAR(50),
    aggregation         VARCHAR(255),
    cast_pattern        VARCHAR(255),
    typical_range       VARCHAR(100),
    operational_meaning TEXT,
    verified            BOOLEAN DEFAULT TRUE,
    provenance          VARCHAR(50),
    created_at          TIMESTAMP DEFAULT NOW(),
    UNIQUE (tenant_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS sql_agent_aliases (
    id              SERIAL PRIMARY KEY,
    tenant_id       INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    alias           VARCHAR(100) NOT NULL,
    canonical_key   VARCHAR(100) NOT NULL,
    alternative_key VARCHAR(100),
    owning_table    VARCHAR(100),
    UNIQUE (tenant_id, alias)
);

CREATE TABLE IF NOT EXISTS sql_agent_business_rules (
    id            SERIAL PRIMARY KEY,
    tenant_id     INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    rule_number   INTEGER NOT NULL,
    rule_text     TEXT NOT NULL,
    doc_reference VARCHAR(255),
    UNIQUE (tenant_id, rule_number)
);

CREATE TABLE IF NOT EXISTS sql_agent_sql_examples (
    id                     SERIAL PRIMARY KEY,
    tenant_id              INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    question               TEXT NOT NULL,
    sql_query              TEXT NOT NULL,
    tags                   VARCHAR(255),
    tables_used            VARCHAR(255),
    intent                 TEXT,
    query_category         VARCHAR(100),
    embedding              VECTOR(1536),
    status                 VARCHAR(20) DEFAULT 'approved',
    provenance_feedback_id INTEGER,
    embedding_model        VARCHAR(50),
    use_count              INTEGER DEFAULT 0,
    last_used_at           TIMESTAMP,
    corrections_after_use  INTEGER DEFAULT 0,
    UNIQUE (tenant_id, question)
);

CREATE TABLE IF NOT EXISTS sql_agent_schema_columns (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    table_name     VARCHAR(100) NOT NULL,
    column_name    VARCHAR(100) NOT NULL,
    datatype       VARCHAR(100) NOT NULL,
    description    TEXT,
    UNIQUE (tenant_id, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS sql_agent_event_types (
    id                    SERIAL PRIMARY KEY,
    tenant_id             INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    event_type            VARCHAR(100) NOT NULL,
    category              VARCHAR(100),
    alert_values          VARCHAR(255),
    description           TEXT,
    event_details_pattern TEXT,
    event_data_schema     TEXT,
    extraction_patterns   TEXT,
    duration_column_note  TEXT,
    UNIQUE (tenant_id, event_type)
);

CREATE TABLE IF NOT EXISTS sql_agent_table_relationships (
    id                   SERIAL PRIMARY KEY,
    tenant_id            INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    source_table         VARCHAR(100) NOT NULL,
    target_table         VARCHAR(100) NOT NULL,
    join_keys            TEXT NOT NULL,
    cardinality          VARCHAR(50),
    recommended_join_type TEXT,
    description          TEXT,
    business_purpose     TEXT,
    notes                TEXT,
    UNIQUE (tenant_id, source_table, target_table)
);

CREATE TABLE IF NOT EXISTS sql_agent_table_metadata (
    id                      SERIAL PRIMARY KEY,
    tenant_id               INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    table_name              VARCHAR(100) NOT NULL,
    fully_qualified_name    VARCHAR(200),
    table_type              VARCHAR(100),
    purpose                 TEXT,
    time_column             VARCHAR(100),
    primary_identifiers     TEXT,
    tenant_scope_column     VARCHAR(100),
    important_notes         TEXT,
    storage_characteristics TEXT,
    UNIQUE (tenant_id, table_name)
);

CREATE TABLE IF NOT EXISTS sql_agent_key_mapping_candidates (
    id             SERIAL PRIMARY KEY,
    tenant_id      INTEGER REFERENCES sql_agent_tenants(id) ON DELETE CASCADE,
    canonical_key  VARCHAR(100),
    physical_key   VARCHAR(100),
    alias          VARCHAR(100),
    confidence     NUMERIC(3,2),
    source_doc     VARCHAR(255),
    status         VARCHAR(20) DEFAULT 'pending',
    extracted_at   TIMESTAMP DEFAULT NOW(),
    reviewed_at    TIMESTAMP,
    UNIQUE (tenant_id, canonical_key, alias)
);
