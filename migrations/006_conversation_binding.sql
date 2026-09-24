-- 006_conversation_binding.sql
--
-- Bind stored conversation material to the exact authorization and semantic
-- context it was produced under.
--
-- Before this, app_conv rows were filtered by owner_user_id alone. That is not
-- sufficient: an answer headline can embed a WAC amount or a territory label,
-- so a user who has since lost pricing permission or been moved to another
-- territory could still read it back through the history and list endpoints --
-- and the conversation TITLE leaked through the list even when the body did
-- not. Ownership answers "whose is it"; it does not answer "may they see it
-- now".
--
-- Additive only: no supplied table is touched.

ALTER TABLE app_conv.conversations
    ADD COLUMN IF NOT EXISTS dataset_id      TEXT,
    ADD COLUMN IF NOT EXISTS metric_version  TEXT,
    ADD COLUMN IF NOT EXISTS policy_version  TEXT;

COMMENT ON COLUMN app_conv.conversations.scope_fingerprint IS
    'Principal.fingerprint() at creation: user, role, scope value and pricing '
    'permission. Continuation AND readback both require it to still match.';
COMMENT ON COLUMN app_conv.conversations.dataset_id IS
    'Published dataset the conversation was started against; a refresh makes '
    'carried plan state incomparable.';

-- Reading history is a per-conversation authorization decision, so make the
-- lookup key the pair rather than the id alone.
CREATE INDEX IF NOT EXISTS ix_conv_owner_fingerprint
    ON app_conv.conversations (owner_user_id, scope_fingerprint);
