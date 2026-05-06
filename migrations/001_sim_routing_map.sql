-- ============================================================
-- Migration 001 — SIM routing map
-- Apply after init.sql
-- ============================================================

-- The only persisted SIM artifact. It maps iccid -> provider/company so
-- single-SIM reads and writes never fan out across all providers.
CREATE TABLE IF NOT EXISTS sim_routing_map (
    iccid        TEXT        PRIMARY KEY,
    provider     TEXT        NOT NULL,
    company_id   UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sim_routing_map_company_idx
    ON sim_routing_map (company_id);

CREATE INDEX IF NOT EXISTS sim_routing_map_company_provider_idx
    ON sim_routing_map (company_id, provider);
