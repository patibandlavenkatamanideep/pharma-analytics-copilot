-- A cohort that was cut off is not the previous result population.
--
-- The pipeline keeps at most 200 ids. Nothing recorded whether that was the
-- whole answer, so a later "those accounts" froze a subset while looking
-- like it froze the whole -- a different population, silently.
ALTER TABLE app_conv.turns
    ADD COLUMN IF NOT EXISTS cohort_complete BOOLEAN,
    ADD COLUMN IF NOT EXISTS cohort_total    INTEGER;

COMMENT ON COLUMN app_conv.turns.cohort_complete IS
    'False when resolved_cohort was truncated at the storage cap. NULL for turns recorded before this column existed: unknown, not complete.';
COMMENT ON COLUMN app_conv.turns.cohort_total IS
    'How many identified rows the answer actually had, before truncation.';
