-- ============================================================
-- Migration 008 — sync_jobs (ADR-012)
-- Apply after 007_company_provider_mappings.sql
--
-- Tracks long-running async jobs produced by the Arq worker:
--   - kind='routing_sync'   → periodic/cron-triggered sync that populates sim_routing_map
--   - kind='export'         → user-triggered massive export of SIM details
--
-- The table is the single source of truth for /v1/jobs/{id} and /v1/sync/* endpoints.
-- It is the queue-independent record (Redis holds the in-flight job; this table holds
-- the durable history and exposes status to clients).
-- ============================================================

CREATE TABLE IF NOT EXISTS sync_jobs (
    id              TEXT        PRIMARY KEY,                          -- arq job id (ulid-like)
    kind            TEXT        NOT NULL,                              -- 'routing_sync' | 'export'
    provider        TEXT,                                              -- nullable: export may span all providers
    company_id      UUID        NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    triggered_by    UUID        REFERENCES users(id) ON DELETE SET NULL, -- nullable for cron-triggered jobs
    status          TEXT        NOT NULL DEFAULT 'pending',            -- 'pending' | 'running' | 'done' | 'failed'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,

    -- Progress (sync only): how many ICCIDs the worker has processed vs estimated total.
    -- 'progress_total' may be null if the provider does not expose a total count upfront.
    progress_done   BIGINT      NOT NULL DEFAULT 0,
    progress_total  BIGINT,

    -- Resumability: the worker persists the last successful cursor here so a restart
    -- can continue from this point (Tele2 modified_since, Kite pageToken, Moabits offset).
    cursor          TEXT,

    -- Export only: where the artifact ended up (S3/blob URL or local volume path).
    -- TTL is enforced out-of-band by a janitor task.
    result_url      TEXT,
    result_expires_at TIMESTAMPTZ,

    -- Structured error log (latest N errors as JSON array of {iccid, kind, message, ts}).
    -- Truncated by the worker; not used as audit (see lifecycle_change_audit/audit_log).
    errors_json     JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Free-form params snapshot of how the job was triggered (filters, fields, format).
    -- Useful for support / re-running the same export.
    params_json     JSONB       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT sync_jobs_kind_chk   CHECK (kind   IN ('routing_sync', 'export')),
    CONSTRAINT sync_jobs_status_chk CHECK (status IN ('pending', 'running', 'done', 'failed'))
);

-- Fast lookup: "show me this tenant's recent jobs" (dashboard / status page).
CREATE INDEX IF NOT EXISTS sync_jobs_company_created_idx
    ON sync_jobs (company_id, created_at DESC);

-- Fast lookup: "what is the last sync per provider for this tenant?" (freshness UI).
CREATE INDEX IF NOT EXISTS sync_jobs_company_kind_provider_finished_idx
    ON sync_jobs (company_id, kind, provider, finished_at DESC)
    WHERE status = 'done';

-- Fast lookup: "any in-flight jobs?" (avoid double-trigger from manual + cron).
CREATE INDEX IF NOT EXISTS sync_jobs_inflight_idx
    ON sync_jobs (company_id, kind, provider)
    WHERE status IN ('pending', 'running');
