-- Incremental ingestion: staging, an event ledger, quarantine, watermarks.
--
-- Additive. The supplied business tables keep their columns and meaning; a
-- sales row created from an event is an ordinary sales row. What is new is
-- the record of WHICH source event each row came from, so that:
--
-- * replaying a batch changes nothing -- an event already applied at the
--   same or a later version is recognised by (source_system, source_event_id),
--   not by comparing amounts. Two sales with equal amounts are two sales;
-- * a correction (a higher event_version) updates the row it corrects, once;
-- * a deletion is a tombstone, so a late copy of the deleted event cannot
--   resurrect it.
--
-- Owned by the owner role and written only by the ingestion job. The serving
-- role can READ batch outcomes and watermarks, to tell users how fresh the
-- data is; it cannot write here.
CREATE SCHEMA IF NOT EXISTS app_ingest;
REVOKE ALL ON SCHEMA app_ingest FROM PUBLIC;

CREATE TABLE IF NOT EXISTS app_ingest.batches (
    source_system       TEXT        NOT NULL,
    batch_id            TEXT        NOT NULL,
    -- The LATEST attempt's outcome. A batch sent again is processed again
    -- (the ledger makes that safe); a replay that changes nothing is
    -- 'no_change' and publishes nothing.
    status              TEXT        NOT NULL CHECK (status IN ('published', 'no_change', 'rejected')),
    attempts            INTEGER     NOT NULL DEFAULT 1,
    first_received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    declared_count      INTEGER     NOT NULL,
    declared_pack_units NUMERIC     NOT NULL,
    received_count      INTEGER     NOT NULL,
    received_pack_units NUMERIC     NOT NULL,
    quarantined         INTEGER     NOT NULL DEFAULT 0,
    applied             INTEGER     NOT NULL DEFAULT 0,
    corrected           INTEGER     NOT NULL DEFAULT 0,
    tombstoned          INTEGER     NOT NULL DEFAULT 0,
    duplicates          INTEGER     NOT NULL DEFAULT 0,
    late                INTEGER     NOT NULL DEFAULT 0,
    affected_periods    TEXT[]      NOT NULL DEFAULT '{}',
    anchor_shift_weeks  INTEGER     NOT NULL DEFAULT 0,
    rejection_reason    TEXT,
    -- The dataset this batch published, if it ever did. Kept when a later
    -- replay of the same batch changes nothing.
    dataset_id          TEXT,
    PRIMARY KEY (source_system, batch_id)
);

CREATE TABLE IF NOT EXISTS app_ingest.event_ledger (
    source_system   TEXT        NOT NULL,
    source_event_id TEXT        NOT NULL,
    event_version   INTEGER     NOT NULL,
    event_time      TIMESTAMPTZ NOT NULL,
    -- A digest of the applied payload: the same id and version arriving with
    -- DIFFERENT content is a source fault, quarantined rather than ignored.
    payload_hash    TEXT        NOT NULL,
    sale_id         BIGINT,                 -- NULL once tombstoned
    tombstoned      BOOLEAN     NOT NULL DEFAULT FALSE,
    first_batch_id  TEXT        NOT NULL,
    last_batch_id   TEXT        NOT NULL,
    applied_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, source_event_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_ledger_sale
    ON app_ingest.event_ledger (sale_id) WHERE sale_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS app_ingest.quarantine (
    source_system   TEXT        NOT NULL,
    batch_id        TEXT        NOT NULL,
    attempt         INTEGER     NOT NULL,
    source_event_id TEXT,
    event_version   INTEGER,
    reason          TEXT        NOT NULL,
    payload         JSONB       NOT NULL,
    quarantined_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (source_system, batch_id) REFERENCES app_ingest.batches (source_system, batch_id)
);
CREATE INDEX IF NOT EXISTS ix_quarantine_batch
    ON app_ingest.quarantine (source_system, batch_id, attempt);

CREATE TABLE IF NOT EXISTS app_ingest.watermarks (
    source_system   TEXT        PRIMARY KEY,
    watermark       TIMESTAMPTZ NOT NULL,     -- latest event time applied
    last_batch_id   TEXT        NOT NULL,
    last_batch_at   TIMESTAMPTZ NOT NULL      -- last batch processed, changed or not
);

GRANT USAGE ON SCHEMA app_ingest TO pac_auth;
GRANT SELECT ON app_ingest.batches, app_ingest.watermarks TO pac_auth;

-- A publication built on top of an earlier one says so. load_mode keeps the
-- base (seed or full): the data is still that dataset, plus increments.
ALTER TABLE app_meta.dataset_manifest
    ADD COLUMN IF NOT EXISTS parent_dataset_id TEXT,
    ADD COLUMN IF NOT EXISTS ingest_batch_id   TEXT;
