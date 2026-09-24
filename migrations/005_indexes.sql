-- 005_indexes.sql
-- Workload-driven indexes only. Each one below exists to serve a query shape the
-- application actually emits; measured EXPLAIN plans and the revisions they
-- prompted are recorded in docs/EVALUATION.md. Indexes are deliberately NOT
-- created for every plausible column -- on a 2M-row write-once table the load
-- cost is real and unused indexes buy nothing.

-- Every scoped query resolves sales -> organizations, and the sales RLS policy
-- probes organizations by org_id.
CREATE INDEX IF NOT EXISTS ix_sales_org_id ON sales (org_id);

-- The near-universal filter: source + company brand + reporting window.
-- Column order follows selectivity in the generated data (data_source splits
-- ~45/5/50, brand_flag is implied by source, mo_offset ranges 0..36).
CREATE INDEX IF NOT EXISTS ix_sales_source_brand_mo
    ON sales (data_source, brand_flag, mo_offset);

-- Market-share denominators filter market_data and join products by NDC.
CREATE INDEX IF NOT EXISTS ix_sales_ndc ON sales (ndc);

-- Weekly windows (R30D / "last 4 weeks").
CREATE INDEX IF NOT EXISTS ix_sales_source_wk ON sales (data_source, wk_offset);

-- Explicit calendar quarter/year grouping and filtering.
CREATE INDEX IF NOT EXISTS ix_sales_period_qtr ON sales (period_qtr);
CREATE INDEX IF NOT EXISTS ix_sales_period_mo  ON sales (period_mo);

-- The organizations RLS policy joins on zip; the reverse direction (find the
-- ZIPs in a territory/region) is what the planner usually prefers for a scoped
-- principal, so both sides are indexed.
CREATE INDEX IF NOT EXISTS ix_org_zip          ON organizations (zip);
CREATE INDEX IF NOT EXISTS ix_zt_territory     ON zip_territory (territory_name);
CREATE INDEX IF NOT EXISTS ix_zt_region        ON zip_territory (region_name);

-- Account rollup to the top-level account and parent-level breakdowns.
CREATE INDEX IF NOT EXISTS ix_org_grandparent  ON organizations (grandparent_org_id);
CREATE INDEX IF NOT EXISTS ix_org_parent       ON organizations (parent_org_id);

-- Product market filters (market share, category analysis).
CREATE INDEX IF NOT EXISTS ix_products_subcat  ON products (market_subcategory);
CREATE INDEX IF NOT EXISTS ix_products_cat     ON products (market_category);
