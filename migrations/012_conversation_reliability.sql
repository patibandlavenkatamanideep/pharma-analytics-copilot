-- Conversation reliability: runs, revisions, full cohorts, pending clarifications.
--
-- Review finding 4. Four defects in how a conversation carried state:
--
-- * Overlapping requests planned against the same stale state. An advisory
--   lock serialised the final INSERT, which fixed sequence collisions, but
--   read -> plan -> execute ran unguarded, so two turns could both continue
--   from turn 3 and both be recorded as if the other had not happened.
-- * A retried request produced a second turn. Nothing identified a request
--   across a retry, so a network failure after the answer was computed meant
--   the user asked again and the conversation recorded the question twice.
-- * A follow-up on a 500-account answer kept 200 ids, and the typed plan's
--   account filter permits only 200. "Those same accounts" became a
--   different, smaller population.
-- * A clarification was not state. "Did you mean A or B?" followed by "the
--   second one" had nothing to resolve against -- and would not survive a
--   restart even if it had.
--
-- Everything here is owner-controlled, user-visible domain state. Workflow
-- execution state (graph checkpoints) is kept separately; a run row is the
-- record both refer to.

-- ---------------------------------------------------------------------------
-- A revision per conversation, checked when a turn is committed
-- ---------------------------------------------------------------------------
-- A request reads the revision when it opens the conversation and commits
-- only if it is unchanged. Planning never holds a transaction open, so this
-- check -- not a lock -- is what detects that the state moved underneath.
ALTER TABLE app_conv.conversations
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- Runs: one row per request. The lease, the idempotency record and the
-- outcome, in one place.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_conv.runs (
    run_id            TEXT        PRIMARY KEY,
    conversation_id   TEXT        NOT NULL REFERENCES app_conv.conversations(conversation_id) ON DELETE CASCADE,
    owner_user_id     TEXT        NOT NULL,
    -- Client-supplied, scoped to the owner. NULL when the client sent
    -- none: the run is then not replayable, only leased.
    idempotency_key   TEXT,
    -- The canonical request. Same key, different payload is a conflict: a
    -- key that could stand for two requests identifies neither.
    payload_hash      TEXT        NOT NULL,
    -- The access the run executed under. A stored outcome is replayed only
    -- while this still equals the caller's current fingerprint, because an
    -- answer headline can carry WAC.
    scope_fingerprint TEXT        NOT NULL,
    base_revision     BIGINT      NOT NULL,
    status            TEXT        NOT NULL CHECK (status IN
                          ('running', 'succeeded', 'failed', 'conflicted', 'abandoned')),
    outcome           JSONB,
    turn_seq          INTEGER,
    lease_expires_at  TIMESTAMPTZ NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ
);

-- One key means one request, per owner. The conversation is part of the
-- request hash rather than of the key's scope: the first turn of a new
-- conversation arrives with no conversation id, and a key scoped by
-- conversation could not recognise its own retry.
DROP INDEX IF EXISTS app_conv.ux_runs_idempotency;
CREATE UNIQUE INDEX ux_runs_idempotency
    ON app_conv.runs (owner_user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- At most one live run per conversation. An expired lease is reclaimed by
-- marking the row abandoned before a new one is inserted, so a crashed
-- worker cannot wedge a conversation for longer than one lease.
CREATE UNIQUE INDEX IF NOT EXISTS ux_runs_one_running
    ON app_conv.runs (conversation_id)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS ix_runs_expiry ON app_conv.runs (expires_at);

-- ---------------------------------------------------------------------------
-- Cohorts: the full, distinct population a follow-up can refer back to
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_conv.cohorts (
    cohort_id         TEXT        PRIMARY KEY,
    conversation_id   TEXT        NOT NULL REFERENCES app_conv.conversations(conversation_id) ON DELETE CASCADE,
    turn_seq          INTEGER     NOT NULL,
    dimension         TEXT        NOT NULL,
    member_count      INTEGER     NOT NULL,
    -- False when the answer itself was truncated: the members are then a
    -- slice, and "those" cannot be frozen as though it were the whole.
    complete          BOOLEAN     NOT NULL,
    dataset_id        TEXT,
    scope_fingerprint TEXT        NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_conv.cohort_members (
    cohort_id  TEXT    NOT NULL REFERENCES app_conv.cohorts(cohort_id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    entity_id  TEXT    NOT NULL,
    -- Distinct by construction: a breakdown by account AND month repeats
    -- each account once per month, and those repeats are not new members.
    PRIMARY KEY (cohort_id, entity_id)
);

ALTER TABLE app_conv.turns
    ADD COLUMN IF NOT EXISTS cohort_id TEXT REFERENCES app_conv.cohorts(cohort_id),
    ADD COLUMN IF NOT EXISTS run_id    TEXT;

-- ---------------------------------------------------------------------------
-- A pending clarification, so a reply can be resolved against it
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_conv.clarifications (
    clarification_id  TEXT        PRIMARY KEY,
    conversation_id   TEXT        NOT NULL REFERENCES app_conv.conversations(conversation_id) ON DELETE CASCADE,
    turn_seq          INTEGER     NOT NULL,
    kind              TEXT        NOT NULL,
    -- The question as first asked, re-run with the chosen entity bound.
    question          TEXT        NOT NULL,
    -- The phrase being clarified, as the user typed it.
    slot_text         TEXT,
    -- Exactly what was shown, in the order shown, so "the second one" means
    -- what the user saw second.
    choices           JSONB       NOT NULL,
    dataset_id        TEXT,
    scope_fingerprint TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending' CHECK (status IN
                          ('pending', 'resolved', 'expired', 'superseded')),
    expires_at        TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_clarification_one_pending
    ON app_conv.clarifications (conversation_id)
    WHERE status = 'pending';

-- ---------------------------------------------------------------------------
-- Grants. GRANT ... ON ALL TABLES in 004 covered the tables that existed
-- then; these are new.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE
    ON app_conv.runs, app_conv.cohorts, app_conv.cohort_members, app_conv.clarifications
    TO pac_auth;
