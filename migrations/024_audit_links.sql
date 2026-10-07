-- 024_audit_links.sql
-- What ties audit rows together (qualification of 7 October 2026, step 5;
-- docs/AUDIT_DECISION.md). Every attempt of a request gets its own
-- request_id; run_id names the run (one per idempotency key) it belongs to.
-- A replay of a committed outcome, recorded under PAC_AUDIT_MODE=strict, is
-- a row of its own whose replay_of names the request that computed it.
-- audit_mode says which contract the row was written under.

ALTER TABLE app_meta.query_audit ADD COLUMN IF NOT EXISTS run_id TEXT;
ALTER TABLE app_meta.query_audit ADD COLUMN IF NOT EXISTS replay_of TEXT;
ALTER TABLE app_meta.query_audit ADD COLUMN IF NOT EXISTS audit_mode TEXT;
CREATE INDEX IF NOT EXISTS ix_audit_run ON app_meta.query_audit (run_id);
