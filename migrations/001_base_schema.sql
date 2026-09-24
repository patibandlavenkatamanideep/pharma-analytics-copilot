-- 001_base_schema.sql
-- PostgreSQL rendering of the supplied schema/create_tables.sql (target: SQLite).
--
-- The supplied DDL file is preserved byte-for-byte and remains the source of truth
-- for the logical schema. This file changes DIALECT ONLY:
--
--   INTEGER PRIMARY KEY AUTOINCREMENT  ->  BIGSERIAL PRIMARY KEY
--   REAL                               ->  DOUBLE PRECISION
--   TEXT / INTEGER                     ->  unchanged
--
-- Table names, column names, column order, nullability, keys and foreign keys are
-- identical. tests/test_schema_parity.py diffs this file against the supplied DDL
-- and fails the build on any drift beyond the dialect map above.

CREATE TABLE IF NOT EXISTS organizations (
    org_id               TEXT PRIMARY KEY,
    org_name             TEXT NOT NULL,
    org_type             TEXT NOT NULL,
    org_status           TEXT NOT NULL,
    org_archetype        TEXT,
    specialty            TEXT,
    address_line1        TEXT,
    city                 TEXT,
    state                TEXT,
    zip                  TEXT,
    parent_org_id        TEXT,
    parent_org_name      TEXT,
    grandparent_org_id   TEXT,
    grandparent_org_name TEXT,
    gpo_name             TEXT,
    is_340b              INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    ndc                    TEXT PRIMARY KEY,
    drug_name              TEXT NOT NULL,
    generic_name           TEXT NOT NULL,
    strength               TEXT,
    form                   TEXT,
    brand_flag             INTEGER NOT NULL,
    specialty              TEXT,
    market_category        TEXT,
    market_subcategory     TEXT,
    unit_conversion_factor DOUBLE PRECISION,
    mg_equivalent          DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id          BIGSERIAL PRIMARY KEY,
    org_id           TEXT NOT NULL,
    ndc              TEXT NOT NULL,
    drug_name        TEXT NOT NULL,
    data_source      TEXT NOT NULL,
    brand_flag       INTEGER NOT NULL,
    pack_units       DOUBLE PRECISION,
    total_mg         DOUBLE PRECISION,
    wac              DOUBLE PRECISION,
    transaction_date TEXT,
    week_ending_date TEXT,
    state            TEXT,
    specialty        TEXT,
    period_wk        TEXT,
    period_mo        TEXT,
    period_qtr       TEXT,
    wk_offset        INTEGER,
    mo_offset        INTEGER,
    FOREIGN KEY (org_id) REFERENCES organizations(org_id),
    FOREIGN KEY (ndc) REFERENCES products(ndc)
);

CREATE TABLE IF NOT EXISTS zip_territory (
    zip              TEXT PRIMARY KEY,
    state            TEXT NOT NULL,
    territory_number TEXT NOT NULL,
    territory_name   TEXT NOT NULL,
    region_number    TEXT,
    region_name      TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    full_name      TEXT NOT NULL,
    role           TEXT NOT NULL,
    territory_name TEXT,
    region_name    TEXT,
    can_view_wac   INTEGER DEFAULT 0
);
