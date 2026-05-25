-- ============================================================
-- Migration 001 — SIM routing map
-- Apply after 000_init.sql
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

-- Prefix routing is a coarse fallback for direct ICCID reads when the exact
-- ICCID has not been observed/imported yet. The exact sim_routing_map row
-- remains the authoritative route whenever it exists.
CREATE TABLE IF NOT EXISTS sim_routing_prefix_map (
    iccid_prefix              TEXT        PRIMARY KEY,
    provider                  TEXT        NOT NULL,
    sample_iccid              TEXT,
    observed_count            BIGINT      NOT NULL DEFAULT 0,
    conflict_count            BIGINT      NOT NULL DEFAULT 0,
    last_conflicting_provider TEXT,
    first_seen_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sim_routing_prefix_map_prefix_chk
        CHECK (iccid_prefix ~ '^[0-9]{6}$')
);

CREATE INDEX IF NOT EXISTS sim_routing_prefix_map_provider_idx
    ON sim_routing_prefix_map (provider);

INSERT INTO sim_routing_prefix_map (iccid_prefix, provider)
VALUES
    ('893407', 'kite'),
    ('893410', 'kite'),
    ('894620', 'tele2'),
    ('891030', 'moabits'),
    ('893571', 'moabits'),
    ('895776', 'moabits')
ON CONFLICT (iccid_prefix) DO UPDATE
SET
    provider = EXCLUDED.provider,
    last_seen_at = now();

WITH normalized AS (
    SELECT
        substring(regexp_replace(iccid, '[^0-9]', '', 'g') FROM 1 FOR 6)
            AS iccid_prefix,
        lower(trim(provider)) AS provider,
        regexp_replace(iccid, '[^0-9]', '', 'g') AS sample_iccid,
        last_seen_at
    FROM sim_routing_map
    WHERE length(regexp_replace(iccid, '[^0-9]', '', 'g')) >= 6
        AND length(trim(provider)) > 0
),
provider_counts AS (
    SELECT
        iccid_prefix,
        provider,
        max(sample_iccid) AS sample_iccid,
        count(*) AS observed_count,
        max(last_seen_at) AS last_seen_at
    FROM normalized
    GROUP BY iccid_prefix, provider
),
ranked AS (
    SELECT
        iccid_prefix,
        provider,
        sample_iccid,
        observed_count,
        greatest(last_seen_at, now()) AS last_seen_at,
        greatest(
            count(*) OVER (PARTITION BY iccid_prefix) - 1,
            0
        ) AS conflict_count,
        row_number() OVER (
            PARTITION BY iccid_prefix
            ORDER BY observed_count DESC, last_seen_at DESC, provider
        ) AS provider_rank
    FROM provider_counts
)
INSERT INTO sim_routing_prefix_map (
    iccid_prefix,
    provider,
    sample_iccid,
    observed_count,
    conflict_count,
    last_seen_at
)
SELECT
    iccid_prefix,
    provider,
    sample_iccid,
    observed_count,
    conflict_count,
    last_seen_at
FROM ranked
WHERE provider_rank = 1
ON CONFLICT (iccid_prefix) DO UPDATE
SET
    sample_iccid = CASE
        WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
            THEN EXCLUDED.sample_iccid
        ELSE sim_routing_prefix_map.sample_iccid
    END,
    observed_count = greatest(
        sim_routing_prefix_map.observed_count,
        EXCLUDED.observed_count
    ),
    conflict_count = greatest(
        sim_routing_prefix_map.conflict_count,
        CASE
            WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
                THEN EXCLUDED.conflict_count
            ELSE EXCLUDED.conflict_count + EXCLUDED.observed_count
        END
    ),
    last_conflicting_provider = CASE
        WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
            THEN sim_routing_prefix_map.last_conflicting_provider
        ELSE EXCLUDED.provider
    END,
    last_seen_at = greatest(
        sim_routing_prefix_map.last_seen_at,
        EXCLUDED.last_seen_at
    );

CREATE OR REPLACE FUNCTION sync_sim_routing_prefix_map()
RETURNS TRIGGER AS $$
DECLARE
    normalized_iccid TEXT;
    normalized_provider TEXT;
    route_prefix TEXT;
BEGIN
    normalized_iccid := regexp_replace(COALESCE(NEW.iccid, ''), '[^0-9]', '', 'g');
    normalized_provider := lower(trim(COALESCE(NEW.provider, '')));

    IF length(normalized_iccid) < 6 OR length(normalized_provider) = 0 THEN
        RETURN NEW;
    END IF;

    route_prefix := substring(normalized_iccid FROM 1 FOR 6);

    INSERT INTO sim_routing_prefix_map (
        iccid_prefix,
        provider,
        sample_iccid,
        observed_count,
        last_seen_at
    )
    VALUES (
        route_prefix,
        normalized_provider,
        normalized_iccid,
        1,
        now()
    )
    ON CONFLICT (iccid_prefix) DO UPDATE
    SET
        sample_iccid = CASE
            WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
                THEN EXCLUDED.sample_iccid
            ELSE sim_routing_prefix_map.sample_iccid
        END,
        observed_count = sim_routing_prefix_map.observed_count + 1,
        conflict_count = CASE
            WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
                THEN sim_routing_prefix_map.conflict_count
            ELSE sim_routing_prefix_map.conflict_count + 1
        END,
        last_conflicting_provider = CASE
            WHEN sim_routing_prefix_map.provider = EXCLUDED.provider
                THEN sim_routing_prefix_map.last_conflicting_provider
            ELSE EXCLUDED.provider
        END,
        last_seen_at = now();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS sim_routing_map_prefix_sync_trg ON sim_routing_map;

CREATE TRIGGER sim_routing_map_prefix_sync_trg
AFTER INSERT OR UPDATE OF iccid, provider ON sim_routing_map
FOR EACH ROW
EXECUTE FUNCTION sync_sim_routing_prefix_map();
