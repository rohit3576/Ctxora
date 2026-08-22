-- Onboarding state table (minimal Phase 2 slice).
-- The full wizard state machine lands in Phase 7; Phase 2 needs only the
-- probe write-through cache that powers the readiness check.

CREATE TABLE IF NOT EXISTS onboarding_state (
    tenant VARCHAR(50) PRIMARY KEY,
    current_step VARCHAR(50) DEFAULT 'probed',
    step_data JSONB,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
