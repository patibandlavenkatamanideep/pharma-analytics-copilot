-- A sign-in in progress belongs to the browser that started it.
--
-- 016 kept state, nonce and PKCE verifier on the server and found them by
-- state alone, so a callback obtained in one browser completed in any other:
-- login CSRF (review of 1 October 2026, R1). Each attempt is now bound to a
-- secret held in an HttpOnly cookie of the starting browser; only its hash
-- is stored here, and the callback must present the secret before the code
-- is exchanged.
ALTER TABLE app_auth.oidc_pending ADD COLUMN IF NOT EXISTS binding_hash TEXT;

-- Attempts begun before this migration have no browser to check against.
-- Each lives ten minutes at most; they are abandoned rather than honoured
-- unbound.
DELETE FROM app_auth.oidc_pending WHERE binding_hash IS NULL;
ALTER TABLE app_auth.oidc_pending ALTER COLUMN binding_hash SET NOT NULL;

-- Several tabs of one browser share a binding; a start looks it up.
CREATE INDEX IF NOT EXISTS ix_oidc_pending_binding ON app_auth.oidc_pending (binding_hash);
