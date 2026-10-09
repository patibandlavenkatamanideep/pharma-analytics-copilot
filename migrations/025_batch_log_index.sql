-- 025_batch_log_index.sql
-- The freshness read (app/data/freshness.py) counts each source's rejected
-- batches since its last accepted one, and its quarantined events in the
-- last day, at every metric collection, under a 500 ms statement timeout.
-- Qualification of 7 October 2026, step 6.
CREATE INDEX IF NOT EXISTS ix_batches_source_attempt
    ON app_ingest.batches (source_system, last_attempt_at);
