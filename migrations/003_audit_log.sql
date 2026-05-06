-- ============================================================
-- Migration 003 — Generic immutable audit log
-- Apply after 002_company_provider_credentials.sql
-- ============================================================

-- Generic append-only audit log for state-changing operations and access
-- denials that need tenant/user traceability.
CREATE TABLE IF NOT EXISTS audit_log (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_id     UUID        REFERENCES users(id) ON DELETE SET NULL,
    company_id   UUID        REFERENCES companies(id) ON DELETE SET NULL,
    action       TEXT        NOT NULL,
    target_type  TEXT,
    target_id    TEXT,
    request_id   TEXT,
    outcome      TEXT        NOT NULL,
    detail       JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_log_company_idx
    ON audit_log (company_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS audit_log_actor_idx
    ON audit_log (actor_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS audit_log_target_idx
    ON audit_log (target_type, target_id);
