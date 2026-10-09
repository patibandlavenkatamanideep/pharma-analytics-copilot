-- 026_model_allowance.sql
-- The website's model allowance (app/llm/allowance.py): one row every serving
-- process -- each uvicorn worker, each replica -- reserves against before a
-- model call and settles after it, in micro-dollars. The limit itself is
-- configuration (PAC_LLM_SPEND_LIMIT_USD); this row is the shared total.
-- Serving requests had no spend bound at all: review of 9 October 2026.
CREATE TABLE IF NOT EXISTS app_meta.model_allowance (
    allowance_id       text        PRIMARY KEY,
    committed_microusd bigint      NOT NULL DEFAULT 0 CHECK (committed_microusd >= 0),
    calls              bigint      NOT NULL DEFAULT 0 CHECK (calls >= 0),
    refused            bigint      NOT NULL DEFAULT 0 CHECK (refused >= 0),
    bound_violations   bigint      NOT NULL DEFAULT 0 CHECK (bound_violations >= 0),
    unreported_calls   bigint      NOT NULL DEFAULT 0 CHECK (unreported_calls >= 0),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

INSERT INTO app_meta.model_allowance (allowance_id) VALUES ('serving')
ON CONFLICT (allowance_id) DO NOTHING;

-- The auth role, which serving processes already hold, reads and updates the
-- counters; it cannot create or delete rows. The analytics roles get nothing.
-- 004_security.sql grants pac_auth INSERT on every app_meta table, so on a
-- second migration pass it would reach this one: revoked here, after it, so
-- one pass and two converge on the same privileges.
GRANT SELECT, UPDATE ON app_meta.model_allowance TO pac_auth;
REVOKE INSERT, DELETE, TRUNCATE ON app_meta.model_allowance FROM pac_auth;
