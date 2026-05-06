-- ============================================================
-- Migration 002 — Provider credentials by company
-- Apply after 001_sim_routing_map.sql
-- ============================================================

-- Encrypted provider credentials per company/provider.
-- credentials_enc contains the Fernet-encrypted JSON secret blob.
-- Kite PFX certificates belong in credentials_enc as base64, never in .env.
-- account_scope stores non-secret metadata only, e.g. environment,
-- end_customer_id, account_id/company_codes, cert_expires_at.
CREATE TABLE IF NOT EXISTS company_provider_credentials (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id       UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    provider         TEXT        NOT NULL,
    credentials_enc  TEXT        NOT NULL,
    account_scope    JSONB       NOT NULL DEFAULT '{}',
    active           BOOLEAN     NOT NULL DEFAULT TRUE,
    rotated_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS company_provider_credentials_active_idx
    ON company_provider_credentials (company_id, provider)
    WHERE active = TRUE;
