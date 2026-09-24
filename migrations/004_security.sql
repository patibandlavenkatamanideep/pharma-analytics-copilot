-- 004_security.sql
-- Database-enforced access control.
--
-- Layering: the plan schema cannot express a WAC metric for a non-Exec principal,
-- the compiler will not emit the identifier, the AST validator rejects it, and --
-- this file -- the runtime role holds no privilege on sales.wac at all. Each layer
-- is independently sufficient, so no single application bug discloses pricing.
--
-- Roles created here are NOLOGIN privilege roles. Login roles with passwords are
-- created by scripts/create_db_roles.py from environment variables, so no secret
-- ever enters git. Each login role is granted exactly one privilege role, which
-- means a scoped connection has no membership path to the Exec role -- SET ROLE
-- cannot escalate it.

-- --------------------------------------------------------------------------
-- Privilege roles
-- --------------------------------------------------------------------------

-- The privilege roles themselves are created by scripts/bootstrap_db.py during
-- its administrative phase, not here. pac_owner intentionally lacks CREATEROLE
-- (it is NOSUPERUSER NOCREATEROLE), so a migration running as the owner cannot
-- mint roles -- which is the property we want: the object owner must not be able
-- to invent new principals. This file only assigns privileges to roles that
-- already exist.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pac_rt_exec')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pac_rt_scoped')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pac_auth') THEN
        RAISE EXCEPTION 'privilege roles missing -- run scripts/bootstrap_db.py first';
    END IF;
END
$$;

-- Nothing is implicitly available.
REVOKE ALL ON ALL TABLES    IN SCHEMA public   FROM PUBLIC;
REVOKE ALL ON SCHEMA public                    FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA app_auth FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA app_conv FROM PUBLIC;
REVOKE ALL ON ALL TABLES    IN SCHEMA app_meta FROM PUBLIC;

GRANT USAGE ON SCHEMA public  TO pac_rt_exec, pac_rt_scoped, pac_auth;
GRANT USAGE ON SCHEMA app_ref TO pac_rt_exec, pac_rt_scoped;

-- --------------------------------------------------------------------------
-- Analytics grants
--
-- Reference data is unrestricted per docs/security_model.md ("The products and
-- zip_territory tables are reference data with no access restrictions").
-- Note that readable reference rows do NOT authorize the sales or organizations
-- rows that join to them -- those carry their own RLS policies below.
-- --------------------------------------------------------------------------

GRANT SELECT ON products                     TO pac_rt_exec, pac_rt_scoped;
GRANT SELECT ON zip_territory                TO pac_rt_exec, pac_rt_scoped;
GRANT SELECT ON app_ref.product_classification TO pac_rt_exec, pac_rt_scoped;
GRANT SELECT ON organizations                TO pac_rt_exec, pac_rt_scoped;

-- Exec may read every column of sales, including wac.
GRANT SELECT ON sales TO pac_rt_exec;

-- Director/RAM get COLUMN-level SELECT that omits wac entirely. There is
-- deliberately no table-level GRANT SELECT ON sales here: a table-level grant
-- would silently supersede the column list and re-expose pricing.
GRANT SELECT (
    sale_id, org_id, ndc, drug_name, data_source, brand_flag,
    pack_units, total_mg, transaction_date, week_ending_date,
    state, specialty, period_wk, period_mo, period_qtr,
    wk_offset, mo_offset
) ON sales TO pac_rt_scoped;

-- The supplied users table and every app_* schema stay unreachable from
-- analytical SQL. No GRANT is issued to the runtime roles for them.
GRANT USAGE  ON SCHEMA app_auth, app_conv, app_meta TO pac_auth;
GRANT SELECT ON users TO pac_auth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app_auth TO pac_auth;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app_conv TO pac_auth;
GRANT SELECT, INSERT                 ON ALL TABLES IN SCHEMA app_meta TO pac_auth;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA app_conv TO pac_auth;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA app_meta TO pac_auth;
-- The auth role needs the manifest to resolve the reporting anchor.
GRANT SELECT ON app_meta.dataset_manifest TO pac_rt_exec, pac_rt_scoped;

-- --------------------------------------------------------------------------
-- Row-level security
--
-- Scope is bound per transaction by trusted server code using
-- set_config('app.scope_kind'|'app.scope_value', ..., is_local => true).
-- Being transaction-local, it cannot leak across a pooled connection: COMMIT or
-- ROLLBACK discards it. A custom GUC is not self-authenticating, so generated
-- SQL is never permitted to call set_config, SET, or any role-changing
-- statement -- only compiler-owned single SELECT statements are executed.
--
-- current_setting(..., true) returns NULL when unset, so every comparison below
-- is false and the default is DENY. An unauthenticated or misconfigured
-- connection sees zero rows rather than everything.
-- --------------------------------------------------------------------------

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales         ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_scope_select   ON organizations;
DROP POLICY IF EXISTS sales_scope_select ON sales;

-- Geography resolves ONLY through zip -> zip_territory, because organizations
-- has no territory or region column. An organization whose ZIP has no mapping
-- row has no provable territory, so a scoped principal cannot see it (fail
-- closed); Exec is global and still sees it, and the answer labels the unmapped
-- geography rather than dropping the rows.
CREATE POLICY org_scope_select ON organizations
    FOR SELECT
    USING (
        current_setting('app.scope_kind', true) = 'global'
        OR EXISTS (
            SELECT 1
            FROM zip_territory z
            WHERE z.zip = organizations.zip
              AND (
                    (current_setting('app.scope_kind', true) = 'region'
                     AND z.region_name = current_setting('app.scope_value', true))
                 OR (current_setting('app.scope_kind', true) = 'territory'
                     AND z.territory_name = current_setting('app.scope_value', true))
              )
        )
    );

-- A sales row is visible exactly when its organization is visible. Referencing
-- organizations here makes its RLS apply transitively, so territory scoping is
-- enforced at the FACILITY row before any hierarchy rollup. A visible
-- grandparent therefore cannot pull in its facilities in other territories --
-- the rollup aggregates only rows that survived this policy.
CREATE POLICY sales_scope_select ON sales
    FOR SELECT
    USING (
        current_setting('app.scope_kind', true) = 'global'
        OR EXISTS (
            SELECT 1 FROM organizations o WHERE o.org_id = sales.org_id
        )
    );
