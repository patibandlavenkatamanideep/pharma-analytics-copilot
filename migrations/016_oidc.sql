-- Standards-based single sign-on, additive to local sign-in.
--
-- identities: a verified (issuer, subject) pair is the permanent key for an
-- external identity. Email is NOT: it changes, it is reassigned, and two
-- providers can assert the same one. A row here is created by an
-- administrator, or -- only if explicitly enabled -- on first sign-in from a
-- provider-verified email.
CREATE TABLE IF NOT EXISTS app_auth.identities (
    issuer        TEXT        NOT NULL,
    subject       TEXT        NOT NULL,
    user_id       TEXT        NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    PRIMARY KEY (issuer, subject)
);
CREATE INDEX IF NOT EXISTS ix_identities_user ON app_auth.identities (user_id);

-- oidc_pending: the state, nonce and PKCE verifier of a sign-in in progress,
-- kept on the server rather than in a signed cookie. Keyed by a hash of the
-- state, single use, short lived.
CREATE TABLE IF NOT EXISTS app_auth.oidc_pending (
    state_hash     TEXT        PRIMARY KEY,
    nonce          TEXT        NOT NULL,
    code_verifier  TEXT        NOT NULL,
    redirect_after TEXT        NOT NULL DEFAULT '/',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE ON app_auth.identities, app_auth.oidc_pending TO pac_auth;
