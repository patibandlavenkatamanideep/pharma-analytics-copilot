-- Idle expiry and token rotation.
--
-- A session had one bound: twelve hours from sign-in. A laptop left open on
-- a desk kept a working session for the rest of the day, and a token that
-- leaked at minute one stayed valid for all twelve hours.
--
-- last_seen_at   -- a session unused for the idle window is dead, whatever
--                   its absolute expiry says.
-- rotated_at     -- when this token was issued, by sign-in or by rotation.
--                   A token older than the rotation window is replaced.
-- superseded_at  -- set on the OLD token when it is rotated. It keeps working
--                   for a few seconds, so requests already in flight with the
--                   old cookie are not signed out mid-answer, and then stops.
--
-- Rotation never moves expires_at: the absolute lifetime is fixed at sign-in.
ALTER TABLE app_auth.sessions
    ADD COLUMN IF NOT EXISTS last_seen_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rotated_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

UPDATE app_auth.sessions SET last_seen_at = created_at WHERE last_seen_at IS NULL;
UPDATE app_auth.sessions SET rotated_at = created_at WHERE rotated_at IS NULL;
