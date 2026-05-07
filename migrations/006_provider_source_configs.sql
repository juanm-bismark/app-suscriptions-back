-- ============================================================
-- Migration 006 — Global provider source configuration
-- Apply after 005_lifecycle_change_audit.sql
-- ============================================================

-- Non-secret provider-wide source configuration. Unlike
-- company_provider_credentials, these settings are not scoped to company_id.
-- Use this for values shared by every local company, such as the selected
-- Moabits company codes for one Moabits source. This table starts empty;
-- configure it through PUT /v1/companies/me/credentials/moabits/company-codes.
CREATE TABLE IF NOT EXISTS provider_source_configs (
    provider    TEXT        PRIMARY KEY,
    settings    JSONB       NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
