-- ============================================================
-- Migration 004 — Idempotency keys for mutating operations
-- Apply after 003_audit_log.sql
-- ============================================================

-- Idempotency keys are scoped per company so two companies can safely send
-- the same client-generated key. The router claims (company_id, key)
-- atomically before calling a provider.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    key        TEXT        NOT NULL,
    response   JSONB       NOT NULL,
    company_id UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idempotency_keys_company_key_uq
    ON idempotency_keys (company_id, key);

CREATE INDEX IF NOT EXISTS idempotency_keys_expires_idx
    ON idempotency_keys (expires_at);
