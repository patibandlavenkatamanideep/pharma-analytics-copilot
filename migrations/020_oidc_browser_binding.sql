-- A sign-in in progress belongs to the browser that started it.
--
-- 016 kept state, nonce and PKCE verifier on the server and found them by
-- state alone, so a callback obtained in one browser completed in any other:
-- login CSRF (review of 1 October 2026, R1). Each attempt is now bound to a
-- secret held in an HttpOnly cookie of the starting browser; only its hash
-- is stored here, and the callback must present the secret before the code
-- is exchanged.
ALTER TABLE app_auth.oidc_pending ADD COLUMN IF NOT EXISTS binding_hash TEXT;

-- Nullable on purpose. This release completes an attempt only when the
-- presented secret's hash EQUALS the stored one, and NULL equals nothing,
-- so an unbound attempt can only be refused. Meanwhile the previous
-- release, which does not know this column, can still start sign-ins if
-- the application is rolled back: NOT NULL with no default would fail every
-- one of its inserts. (The first draft of this migration set NOT NULL;
-- databases that applied it are put back.)
ALTER TABLE app_auth.oidc_pending ALTER COLUMN binding_hash DROP NOT NULL;

-- Several tabs of one browser share a binding; a start looks it up.
CREATE INDEX IF NOT EXISTS ix_oidc_pending_binding ON app_auth.oidc_pending (binding_hash);
