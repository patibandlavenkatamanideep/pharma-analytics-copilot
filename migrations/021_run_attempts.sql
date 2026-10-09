-- Every attempt that does work, for the per-user rate limits.
--
-- 015 counted the rate windows from app_conv.runs.created_at. A run that
-- failed, lost a revision race, was abandoned or was cancelled is run again
-- under the same key -- and that reclaim was neither checked nor counted,
-- while deleting a conversation (runs cascade with it) refunded its share
-- (review of 1 October 2026, R2).
--
-- One row per attempt, written in the same transaction that admits it. Not a
-- foreign key to runs: deleting a conversation must not refund what it
-- cost. Only timestamps and ids -- no question, no answer -- kept for the
-- longest rate window plus a margin (app/conversation/runs.py prunes a
-- user's old rows as it counts; app/retention.py prunes everyone's).
CREATE TABLE IF NOT EXISTS app_conv.run_attempts (
    attempt_id     BIGSERIAL   PRIMARY KEY,
    owner_user_id  TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    run_id         TEXT        NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_run_attempts_owner_time
    ON app_conv.run_attempts (owner_user_id, started_at);

-- Runs admitted before this table existed still count for the rest of the
-- hour, so deploying it does not hand everyone a fresh allowance.
INSERT INTO app_conv.run_attempts (owner_user_id, run_id, started_at)
SELECT r.owner_user_id, r.run_id, r.created_at
FROM app_conv.runs r
WHERE r.created_at > now() - interval '1 hour'
  AND NOT EXISTS (SELECT 1 FROM app_conv.run_attempts a WHERE a.run_id = r.run_id);

GRANT SELECT, INSERT, DELETE ON app_conv.run_attempts TO pac_auth;
GRANT USAGE ON SEQUENCE app_conv.run_attempts_attempt_id_seq TO pac_auth;
-- 004 grants every privilege on every app_conv table on each pass -- on the
-- first pass before this table exists, on later passes after. The serving
-- role records, counts and prunes attempts; it never rewrites one. Revoked
-- here, after 004 on every pass, so one pass and two end the same
-- (tests/security/test_fresh_provisioning.py).
REVOKE UPDATE ON app_conv.run_attempts FROM pac_auth;
