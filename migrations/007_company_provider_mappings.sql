-- ============================================================
-- Migration 007 — Company-scoped provider mappings
-- Apply after 006_moabits_source_companies.sql
-- ============================================================

-- Links a local company to a provider-native company/account. For Moabits,
-- provider_company_code is the companyCode used for SIM requests; optional
-- discovery cache rows in moabits_source_companies only help present choices.
CREATE TABLE IF NOT EXISTS company_provider_mappings (
    company_id             UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    provider               TEXT        NOT NULL,
    provider_company_code  TEXT        NOT NULL,
    provider_company_name  TEXT,
    clie_id                INTEGER,
    settings               JSONB       NOT NULL DEFAULT '{}',
    active                 BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, provider)
);

CREATE INDEX IF NOT EXISTS company_provider_mappings_provider_code_idx
    ON company_provider_mappings (provider, provider_company_code);
