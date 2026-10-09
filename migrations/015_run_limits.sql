-- Cancellation, and the indexes per-user limits read.
--
-- A request can now be cancelled by its owner while it runs. The run row
-- carries the request; the graph checks it between steps and stops at the
-- next boundary. A statement already executing is bounded by its timeout,
-- not interrupted mid-scan.
ALTER TABLE app_conv.runs
    ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;

ALTER TABLE app_conv.runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE app_conv.runs ADD CONSTRAINT runs_status_check CHECK (status IN
    ('running', 'succeeded', 'failed', 'conflicted', 'abandoned', 'cancelled'));

-- Per-user request rate and concurrency are counted from runs, in the
-- database, so the limit holds across every worker and replica -- a counter
-- in process memory would give each worker its own allowance.
CREATE INDEX IF NOT EXISTS ix_runs_owner_created
    ON app_conv.runs (owner_user_id, created_at DESC);
