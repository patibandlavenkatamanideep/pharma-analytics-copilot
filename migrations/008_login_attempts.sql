-- Failed sign-in attempts, so repeated guessing can be slowed down.
--
-- Without this a password could be tried without limit and without trace: the
-- deliberately identical failure message hides WHICH part was wrong from an
-- attacker, but it does nothing to stop them trying again.
CREATE TABLE IF NOT EXISTS app_auth.login_attempts (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT        NOT NULL,
    ip_hash     TEXT,
    succeeded   BOOLEAN     NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The lookup is always "recent failures for this identity", so the index leads
-- with the window.
CREATE INDEX IF NOT EXISTS ix_login_attempts_recent
    ON app_auth.login_attempts (attempted_at DESC, email);
CREATE INDEX IF NOT EXISTS ix_login_attempts_ip
    ON app_auth.login_attempts (attempted_at DESC, ip_hash);

COMMENT ON TABLE app_auth.login_attempts IS
    'Sign-in attempts. Holds an email and a hashed IP, never a password.';

-- The serving role records every attempt. Granted HERE, explicitly: the
-- blanket "ON ALL TABLES IN SCHEMA app_auth" grant in 004 runs before this
-- table exists, so on a database provisioned in one pass the role could not
-- read or write it and every sign-in failed. Databases that had been
-- migrated twice picked the table up on the second pass, and older ones
-- carried a hand-applied sequence grant on the login role, which is why
-- nothing local caught it; scripts/image_smoke.sh did, on a fresh database.
GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.login_attempts TO pac_auth;
GRANT USAGE ON SEQUENCE app_auth.login_attempts_id_seq TO pac_auth;
