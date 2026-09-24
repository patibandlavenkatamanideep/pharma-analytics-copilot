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
