-- 003_derived_classification.sql
-- DERIVED reference data -- not supplied by the assignment.
--
-- brand_flag = 0 means "competitor/generic" and covers branded competitors
-- (TAXOTERE, GEMZAR, ALIMTA, KEYTRUDA, AVASTIN ...). There is no generic or
-- biosimilar column anywhere in the supplied schema, so a question like
-- "what is the generic share in Platinum Compounds" cannot be answered from
-- brand_flag alone without silently relabelling all competitor volume as generic.
--
-- This table makes the classification explicit, auditable and versioned. Answers
-- that use it disclose that the classification is derived. Rows are populated by
-- the loader from a documented drug-name rule (see app/data/classification.py):
--   name ends with ' GENERIC'     -> 'generic'
--   name ends with ' BIOSIMILAR'  -> 'biosimilar'
--   brand_flag = 1                -> 'company_brand'
--   otherwise                     -> 'branded_competitor'

CREATE TABLE IF NOT EXISTS app_ref.product_classification (
    ndc            TEXT PRIMARY KEY REFERENCES products(ndc),
    classification TEXT NOT NULL CHECK (classification IN
                        ('company_brand', 'branded_competitor', 'generic', 'biosimilar')),
    derivation     TEXT NOT NULL,   -- which rule fired, for audit
    rule_version   TEXT NOT NULL
);
