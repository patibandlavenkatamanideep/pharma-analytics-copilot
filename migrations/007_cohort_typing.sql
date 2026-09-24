-- A cohort is a set of ids AT A GRAIN. Storing the ids without the grain let
-- a product cohort be reapplied as account ids on the next turn, which filters
-- organizations by drug name and matches nothing.
ALTER TABLE app_conv.turns
    ADD COLUMN IF NOT EXISTS cohort_dimension TEXT;

COMMENT ON COLUMN app_conv.turns.cohort_dimension IS
    'The dimension resolved_cohort holds ids for. NULL means no cohort.';
