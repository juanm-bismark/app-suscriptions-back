-- ============================================================
-- Migration 005 — Lifecycle write audit
-- Apply after 004_idempotency_keys.sql
-- ============================================================

-- Fine-grained audit record for SIM lifecycle writes (status changes and
-- canonical purge). Success, replay, and failure attempts are all recorded.
CREATE TABLE IF NOT EXISTS lifecycle_change_audit (
    id                  BIGSERIAL PRIMARY KEY,
    company_id          TEXT,
    actor_id            TEXT,
    request_id          TEXT,
    iccid               TEXT        NOT NULL,
    provider            TEXT        NOT NULL,
    action              TEXT        NOT NULL,
    target              TEXT,
    idempotency_key     TEXT,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at         TIMESTAMPTZ NULL,
    outcome             TEXT        NOT NULL DEFAULT 'unknown',
    latency_ms          INTEGER,
    provider_request_id TEXT,
    provider_error_code TEXT,
    error               TEXT,
    meta                JSONB
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_audit_provider_requested_at
    ON lifecycle_change_audit(provider, requested_at);

CREATE INDEX IF NOT EXISTS idx_lifecycle_audit_company_requested_at
    ON lifecycle_change_audit(company_id, requested_at);

CREATE INDEX IF NOT EXISTS idx_lifecycle_audit_request_id
    ON lifecycle_change_audit(request_id);
