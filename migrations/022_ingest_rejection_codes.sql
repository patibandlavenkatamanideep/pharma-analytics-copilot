-- A stable code for why a batch was rejected, beside the human-readable
-- reason (review of 1 October 2026, R5). Alerts and reports match on the
-- code; the reason's wording can change. Codes: malformed_document,
-- invalid_envelope, control_totals, quality_threshold, no_parent_dataset,
-- calendar_unextendable, apply_failed (docs/INGESTION.md).
ALTER TABLE app_ingest.batches ADD COLUMN IF NOT EXISTS rejection_code TEXT;
