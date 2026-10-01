-- Feedback on answers, from the person who asked.
--
-- Owner-scoped like the conversation it belongs to, and deleted with it
-- (ON DELETE CASCADE), so a user's deletion request takes it too. One row per
-- turn: changing your mind replaces it. The comment is the user's own text
-- and is treated like a question -- never exported to telemetry, and left out
-- of the triage sample unless an operator asks for it explicitly.
CREATE TABLE IF NOT EXISTS app_conv.feedback (
    conversation_id TEXT        NOT NULL
                    REFERENCES app_conv.conversations (conversation_id) ON DELETE CASCADE,
    turn_seq        INTEGER     NOT NULL,
    owner_user_id   TEXT        NOT NULL,
    run_id          TEXT        NOT NULL,
    rating          SMALLINT    NOT NULL CHECK (rating IN (-1, 1)),
    reason          TEXT        CHECK (reason IN ('wrong_number', 'different_question',
                                                  'missing_data', 'too_slow', 'other')),
    comment         TEXT        CHECK (length(comment) <= 500),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, turn_seq)
);
CREATE INDEX IF NOT EXISTS ix_feedback_created ON app_conv.feedback (created_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON app_conv.feedback TO pac_auth;
