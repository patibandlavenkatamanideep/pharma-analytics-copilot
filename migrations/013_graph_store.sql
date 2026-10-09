-- Durable workflow state for the orchestration graph.
--
-- The checkpointer's own tables (checkpoints, checkpoint_blobs,
-- checkpoint_writes, checkpoint_migrations) are created by LangGraph's
-- PostgresSaver.setup(), the supported path for its schema -- run by
-- scripts/setup_graph_store.py as the OWNER, never by the serving process.
-- This file creates the schema they live in and the runtime grants.
--
-- Kept in its own schema, apart from app_conv: a checkpoint is workflow
-- execution state (where a run is, what it has decided), app_conv is the
-- user-visible domain record. They are related by run id and never by
-- copying one into the other.
CREATE SCHEMA IF NOT EXISTS app_graph;
REVOKE ALL ON SCHEMA app_graph FROM PUBLIC;
GRANT USAGE ON SCHEMA app_graph TO pac_auth;

-- Tables created later by setup() are granted to the runtime role
-- automatically. DML only: the serving process can record and read
-- workflow state, and cannot create, alter or drop anything.
ALTER DEFAULT PRIVILEGES IN SCHEMA app_graph
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pac_auth;

-- An audit row per request, once. Graph nodes can re-execute after a crash;
-- the audit write is keyed so a replay cannot record a request twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_query_audit_request
    ON app_meta.query_audit (request_id);

-- A pending clarification is a paused graph thread. The domain row names it,
-- so a reply resumes that thread -- after the owner check the domain row
-- already passed.
ALTER TABLE app_conv.clarifications
    ADD COLUMN IF NOT EXISTS graph_thread_id TEXT;
