-- 002_app_schema.sql
-- Additive application tables. These live in separate schemas so that no
-- analytical query can reach them: the analytics runtime roles are never granted
-- USAGE on app_auth, app_conv or app_meta, and the compiler's relation allowlist
-- contains only the five supplied tables plus app_ref.product_classification.

CREATE SCHEMA IF NOT EXISTS app_auth;   -- credentials and sessions
CREATE SCHEMA IF NOT EXISTS app_conv;   -- user-owned conversation state
CREATE SCHEMA IF NOT EXISTS app_meta;   -- dataset manifest and query audit
CREATE SCHEMA IF NOT EXISTS app_ref;    -- derived reference data (readable by analytics)

-- --------------------------------------------------------------------------
-- Authentication. The supplied users table is NOT modified; it stays the
-- authority for role, assignment and can_view_wac. It has no secret column, so
-- credentials are stored here and joined by user_id.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_auth.credentials (
    user_id       TEXT PRIMARY KEY REFERENCES users(user_id),
    password_hash TEXT        NOT NULL,          -- argon2id
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled      BOOLEAN     NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS app_auth.sessions (
    -- SHA-256 of the opaque token. The token itself is never stored, so a dump
    -- of this table cannot be replayed as a session.
    token_hash  TEXT        PRIMARY KEY,
    user_id     TEXT        NOT NULL REFERENCES users(user_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    user_agent  TEXT,
    ip_hash     TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user    ON app_auth.sessions (user_id);
CREATE INDEX IF NOT EXISTS ix_sessions_expires ON app_auth.sessions (expires_at);

-- --------------------------------------------------------------------------
-- Conversations. Structured plan state only -- never raw SQL and never rows of
-- results. owner_user_id is checked on every read; a known conversation id is
-- not sufficient to open someone else's thread.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_conv.conversations (
    conversation_id TEXT        PRIMARY KEY,
    owner_user_id   TEXT        NOT NULL REFERENCES users(user_id),
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Scope fingerprint at creation. If the owner's effective scope changes,
    -- carried-over plan state is invalidated rather than silently reused.
    scope_fingerprint TEXT      NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conv_owner ON app_conv.conversations (owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS app_conv.turns (
    turn_id         BIGSERIAL   PRIMARY KEY,
    conversation_id TEXT        NOT NULL REFERENCES app_conv.conversations(conversation_id) ON DELETE CASCADE,
    seq             INTEGER     NOT NULL,
    question        TEXT        NOT NULL,
    -- The validated typed plan, as JSON. This is what a follow-up patches.
    plan            JSONB,
    -- Resolved entity ids the answer was about, so "those five accounts" is a
    -- frozen cohort rather than a re-ranked one.
    resolved_cohort JSONB,
    answer_text     TEXT,
    status          TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
);

-- --------------------------------------------------------------------------
-- Dataset manifest. A snapshot is queryable only when load_state = 'published',
-- so a partial refresh is never visible to users.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_meta.dataset_manifest (
    dataset_id        TEXT        PRIMARY KEY,
    load_mode         TEXT        NOT NULL CHECK (load_mode IN ('seed', 'full')),
    load_state        TEXT        NOT NULL CHECK (load_state IN ('loading', 'published', 'failed', 'superseded')),
    schema_version    TEXT        NOT NULL,
    mapping_version   TEXT        NOT NULL,
    source_hashes     JSONB       NOT NULL,
    row_counts        JSONB       NOT NULL,
    -- Reporting anchor, derived from the data itself, never from the wall clock.
    reporting_anchor  JSONB       NOT NULL,
    source_coverage   JSONB       NOT NULL,
    warnings          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at      TIMESTAMPTZ
);

-- --------------------------------------------------------------------------
-- Query audit. Deliberately stores hashes and counts, never WAC values, never
-- result rows, never raw prompts.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_meta.query_audit (
    audit_id        BIGSERIAL   PRIMARY KEY,
    request_id      TEXT        NOT NULL,
    user_id         TEXT        NOT NULL,
    role            TEXT        NOT NULL,
    scope_kind      TEXT        NOT NULL,
    scope_value     TEXT,
    wac_authorized  BOOLEAN     NOT NULL,
    dataset_id      TEXT,
    metric_version  TEXT,
    policy_version  TEXT,
    plan_hash       TEXT,
    sql_hash        TEXT,
    status          TEXT        NOT NULL,
    denial_reason   TEXT,
    row_count       INTEGER,
    db_ms           INTEGER,
    total_ms        INTEGER,
    model_id        TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_user_time ON app_meta.query_audit (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_status    ON app_meta.query_audit (status, created_at DESC);
