-- Fields the pipeline recorded and the audit trail never received.
--
-- The review found intent_gaps set on the in-memory audit record and absent
-- from this table. Checking every key against the column list found five
-- more, added by later work: the turn classification, the prompt version,
-- the planner attempt count, whether a repair happened, and whether usage
-- was known at all. Each was built, carried through the request, and
-- discarded at the INSERT.
ALTER TABLE app_meta.query_audit
    ADD COLUMN IF NOT EXISTS reason_codes     TEXT[],
    ADD COLUMN IF NOT EXISTS intent_gaps      TEXT[],
    ADD COLUMN IF NOT EXISTS turn_kind        TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version   TEXT,
    ADD COLUMN IF NOT EXISTS planner_attempts INTEGER,
    ADD COLUMN IF NOT EXISTS planner_repaired BOOLEAN,
    ADD COLUMN IF NOT EXISTS usage_known      BOOLEAN;

COMMENT ON COLUMN app_meta.query_audit.reason_codes IS
    'Machine-readable why: the outcome code, then every blocking gap. Codes only -- never values, names or text from the question.';
COMMENT ON COLUMN app_meta.query_audit.intent_gaps IS
    'Every gap between question and plan, blocking or disclosed, by kind.';
COMMENT ON COLUMN app_meta.query_audit.usage_known IS
    'False when the provider reported no usage: the token columns are unknown, not zero. NULL for rows written before this column existed.';
