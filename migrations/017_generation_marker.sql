-- The published generation, readable by the roles that read the facts.
--
-- Review finding 6: a request read the manifest, the vocabulary and the facts
-- in separate transactions, so a refresh between them could plan against one
-- generation and execute against another -- last quarter's calendar applied
-- to this quarter's rows, with nothing to notice it.
--
-- The analytics roles deliberately cannot read app_meta, so the generation is
-- mirrored here, in the same transaction that publishes the facts. A query
-- transaction reads this row and the facts under ONE repeatable-read
-- snapshot: if the row names the generation the request planned against, so
-- do the facts. Publication is MVCC-safe (DELETE, not TRUNCATE), so an older
-- snapshot keeps seeing the older generation whole.
CREATE TABLE IF NOT EXISTS app_ref.generation (
    singleton    BOOLEAN     PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    dataset_id   TEXT        NOT NULL,
    published_at TIMESTAMPTZ NOT NULL
);

INSERT INTO app_ref.generation (singleton, dataset_id, published_at)
SELECT TRUE, dataset_id, published_at FROM app_meta.dataset_manifest
WHERE load_state = 'published'
ORDER BY published_at DESC LIMIT 1
ON CONFLICT (singleton) DO UPDATE
    SET dataset_id = EXCLUDED.dataset_id, published_at = EXCLUDED.published_at;

GRANT SELECT ON app_ref.generation TO pac_rt_exec, pac_rt_scoped, pac_auth;
