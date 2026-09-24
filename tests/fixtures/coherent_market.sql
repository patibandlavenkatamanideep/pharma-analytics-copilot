-- A SMALL, HAND-CALCULATED FIXTURE -- NOT the supplied dataset.
--
-- The supplied data cannot exercise the market-share contract as documented:
-- every market_data row has brand_flag = 0, so the denominator is a
-- competitor-only total and every company share exceeds 100%. That is a real
-- property of the baseline and is reported honestly rather than repaired
-- (docs/ASSUMPTIONS.md#a3).
--
-- This fixture is the counterpart: a coherent market where market_data DOES
-- include the company's own volume, so the intended formula can be verified
-- end to end. It is kept strictly separate from baseline evaluation and is
-- never loaded into the application database.
--
-- Hand-calculated expectations (all within mo_offset 0-2):
--
--   ZENOVAX distributor equivalents:
--     ACME-1: 50 packs x 1.0  =  50
--     ACME-2: 20 packs x 1.0  =  20        (Texas facility)
--     ACME-3: 40 packs x 0.25 =  10        (20MG vial, factor 0.25)
--                              -----
--                                80 equivalents
--
--   Docetaxel market_data equivalents (INCLUDES the company brand):
--     ZENOVAX  in market data:  30 x 1.0  =  30
--     TAXOTERE               : 100 x 1.0  = 100
--     DOCETAXEL GENERIC      :  70 x 1.0  =  70
--                                          -----
--                                            200 equivalents
--
--   Market share = 80 / 200 = 0.40 exactly.
--
--   Hub (free drug) adds 20 packs of ZENOVAX. Paid demand and market share
--   must NOT change; total volume including free drug becomes 110 + 20.
--
--   The standalone, unmapped and inactive facilities deliberately carry a
--   DIFFERENT product (NOVATAXEL), so they exercise hierarchy, geography and
--   status without perturbing the 80/200 Docetaxel arithmetic above.
--
--   Carboplatin has market volume but no company volume: share must be 0 or
--   NULL, never an error.
--   Paclitaxel has company volume but no market volume: denominator 0, so the
--   share must be NULL -- never a division error and never 0%.

TRUNCATE sales, organizations, products, zip_territory RESTART IDENTITY CASCADE;

-- --------------------------------------------------------------------------
-- Geography: two territories in two different regions, plus one unmapped ZIP.
-- --------------------------------------------------------------------------
INSERT INTO zip_territory (zip, state, territory_number, territory_name, region_number, region_name) VALUES
  ('10001', 'NY', 'T001', 'New York Metro', 'R01', 'Northeast'),
  ('75001', 'TX', 'T010', 'Texas',          'R05', 'South Central');
-- NOTE: 99999 is deliberately absent, to exercise fail-closed behaviour.

-- --------------------------------------------------------------------------
-- Organizations: one health system spanning TWO territories, one standalone,
-- one 340B facility, and one facility whose ZIP has no mapping.
-- --------------------------------------------------------------------------
INSERT INTO organizations
  (org_id, org_name, org_type, org_status, org_archetype, specialty,
   city, state, zip, parent_org_id, parent_org_name,
   grandparent_org_id, grandparent_org_name, gpo_name, is_340b) VALUES
  ('GP1', 'Acme Health System', 'Grandparent', 'Active', 'IDN', 'Oncology',
   'New York', 'NY', '10001', NULL, NULL, NULL, NULL, 'Onmark', 0),
  ('ACME-1', 'Acme Midtown Infusion', 'Facility', 'Active', 'Hospital', 'Oncology',
   'New York', 'NY', '10001', NULL, NULL, 'GP1', 'Acme Health System', 'Onmark', 0),
  -- Same health system, DIFFERENT territory. A RAM in New York Metro must not
  -- see this facility even though its grandparent is visible to them.
  ('ACME-2', 'Acme Dallas Infusion', 'Facility', 'Active', 'Hospital', 'Oncology',
   'Dallas', 'TX', '75001', NULL, NULL, 'GP1', 'Acme Health System', 'Onmark', 0),
  -- 340B facility, same system, same territory as ACME-1.
  ('ACME-3', 'Acme Safety Net Clinic', 'Facility', 'Active', 'Clinic', 'Oncology',
   'New York', 'NY', '10001', NULL, NULL, 'GP1', 'Acme Health System', 'Onmark', 1),
  -- Standalone facility: no parent, no grandparent. Its own top-level account.
  ('SOLO-1', 'Independent Cancer Center', 'Facility', 'Active', 'Clinic', 'Oncology',
   'New York', 'NY', '10001', NULL, NULL, NULL, NULL, NULL, 0),
  -- Deliberate duplicate NAME with a different id, to prove grouping is by id.
  ('SOLO-2', 'Independent Cancer Center', 'Facility', 'Active', 'Clinic', 'Oncology',
   'Dallas', 'TX', '75001', NULL, NULL, NULL, NULL, NULL, 0),
  -- Unmapped ZIP: scoped roles must not see it; Exec must still count it.
  ('LOST-1', 'Unmapped Clinic', 'Facility', 'Active', 'Clinic', 'Oncology',
   'Nowhere', 'ZZ', '99999', NULL, NULL, NULL, NULL, NULL, 0),
  -- Inactive facility, to separate "historical volume" from "active accounts".
  ('OLD-1', 'Closed Infusion Center', 'Facility', 'Inactive', 'Clinic', 'Oncology',
   'New York', 'NY', '10001', NULL, NULL, NULL, NULL, NULL, 0);

-- --------------------------------------------------------------------------
-- Products: two strengths of the company brand (different conversion factors),
-- two competitors, and two other subcategories for the edge cases.
-- --------------------------------------------------------------------------
INSERT INTO products
  (ndc, drug_name, generic_name, strength, form, brand_flag, specialty,
   market_category, market_subcategory, unit_conversion_factor, mg_equivalent) VALUES
  ('11111-0101-01', 'ZENOVAX',           'docetaxel',  '80MG/4ML', 'Injectable', 1, 'Oncology',
   'Taxanes', 'Docetaxel', 1.0, 80.0),
  ('11111-0101-02', 'ZENOVAX',           'docetaxel',  '20MG/1ML', 'Injectable', 1, 'Oncology',
   'Taxanes', 'Docetaxel', 0.25, 20.0),
  ('22222-0101-01', 'TAXOTERE',          'docetaxel',  '80MG/4ML', 'Injectable', 0, 'Oncology',
   'Taxanes', 'Docetaxel', 1.0, 80.0),
  ('22222-0102-01', 'DOCETAXEL GENERIC', 'docetaxel',  '80MG/4ML', 'Injectable', 0, 'Oncology',
   'Taxanes', 'Docetaxel', 1.0, 80.0),
  -- Market volume but no company volume.
  ('22222-0201-01', 'PARAPLATIN',        'carboplatin', '450MG',   'Injectable', 0, 'Oncology',
   'Platinum Compounds', 'Carboplatin', 1.0, 450.0),
  -- Company volume but no market volume -> zero denominator.
  ('11111-0801-01', 'NOVATAXEL',         'paclitaxel',  '100MG',   'Injectable', 1, 'Oncology',
   'Taxanes', 'Paclitaxel', 1.0, 100.0);

-- --------------------------------------------------------------------------
-- Sales. mo_offset 0-2 is the R3M window; offset 3 exists so a prior-period
-- comparison has something to compare against.
-- --------------------------------------------------------------------------

-- Company paid demand (distributor): 50 + 20 + 10 = 80 equivalents, 110 packs.
INSERT INTO sales
  (org_id, ndc, drug_name, data_source, brand_flag, pack_units, total_mg, wac,
   transaction_date, week_ending_date, state, specialty,
   period_wk, period_mo, period_qtr, wk_offset, mo_offset) VALUES
  ('ACME-1', '11111-0101-01', 'ZENOVAX', 'distributor', 1, 50, 4000, 5000.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),
  ('ACME-2', '11111-0101-01', 'ZENOVAX', 'distributor', 1, 20, 1600, 2000.00,
   '2026-09-14', '2026-09-19', 'TX', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),
  ('ACME-3', '11111-0101-02', 'ZENOVAX', 'distributor', 1, 40,  800, 1000.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Free drug: must not affect paid demand or market share.
  ('ACME-1', '11111-0101-01', 'ZENOVAX', 'hub_dispense', 1, 20, 1600, 0.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Market data for Docetaxel, INCLUDING the company brand: 30 + 100 + 70 = 200.
  ('ACME-1', '11111-0101-01', 'ZENOVAX',           'market_data', 1,  30, 2400, 0,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),
  ('ACME-1', '22222-0101-01', 'TAXOTERE',          'market_data', 0, 100, 8000, 0,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),
  ('ACME-1', '22222-0102-01', 'DOCETAXEL GENERIC', 'market_data', 0,  70, 5600, 0,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Carboplatin: market volume, no company volume.
  ('ACME-1', '22222-0201-01', 'PARAPLATIN', 'market_data', 0, 25, 11250, 0,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Paclitaxel: company volume, no market volume -> zero denominator.
  ('ACME-1', '11111-0801-01', 'NOVATAXEL', 'distributor', 1, 5, 500, 750.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Standalone facilities, including the duplicate-name pair.
  ('SOLO-1', '11111-0801-01', 'NOVATAXEL', 'distributor', 1, 7, 700, 700.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 1),
  ('SOLO-2', '11111-0801-01', 'NOVATAXEL', 'distributor', 1, 9, 900, 900.00,
   '2026-09-14', '2026-09-19', 'TX', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 1),

-- Unmapped ZIP: 11 packs that scoped roles must never see.
  ('LOST-1', '11111-0801-01', 'NOVATAXEL', 'distributor', 1, 11, 1100, 1100.00,
   '2026-09-14', '2026-09-19', 'ZZ', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 0),

-- Inactive facility still has historical volume.
  ('OLD-1', '11111-0801-01', 'NOVATAXEL', 'distributor', 1, 13, 1300, 1300.00,
   '2026-09-14', '2026-09-19', 'NY', 'Oncology', '2026-W38', '2026-09', '2026-Q3', 0, 2),

-- Prior period (offset 3) for comparison metrics: 40 equivalents, market 160.
  ('ACME-1', '11111-0101-01', 'ZENOVAX',  'distributor', 1, 40, 3200, 4000.00,
   '2026-06-15', '2026-06-20', 'NY', 'Oncology', '2026-W25', '2026-06', '2026-Q2', 13, 3),
  ('ACME-1', '22222-0101-01', 'TAXOTERE', 'market_data', 0, 160, 12800, 0,
   '2026-06-15', '2026-06-20', 'NY', 'Oncology', '2026-W25', '2026-06', '2026-Q2', 13, 3);
