-- ============================================================
-- Migration 006 — Moabits discovered company cache
-- Apply after 005_lifecycle_change_audit.sql
-- ============================================================

-- Cache of companies returned by Moabits discovery for a credential/source
-- company. This table is for UI discovery, autocomplete, and avoiding repeated
-- provider calls; company_provider_mappings remains the source of truth for
-- which local company can request SIMs for a Moabits company code.
CREATE TABLE IF NOT EXISTS moabits_source_companies (
    source_company_id  UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    company_code       TEXT        NOT NULL,
    company_name       TEXT        NOT NULL DEFAULT '',
    clie_id            INTEGER,
    raw_payload        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    active             BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_company_id, company_code)
);
