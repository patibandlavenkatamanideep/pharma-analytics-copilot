-- 023_classification_authority.sql
-- Where each product's class comes from (qualification of 7 October 2026,
-- step 4; app/data/classification.py). Rule 2.0.0: brand_flag = 1 is
-- company_brand (authority 'source'); otherwise the dataset's classification
-- mapping (authority 'mapping', mapping_ref names its version and digest);
-- otherwise 'unknown' (authority 'none'). Rule 1.0.0 classed every other
-- brand_flag = 0 product as a branded competitor by elimination; rows it
-- wrote keep authority 'rule' until the next load replaces them.

ALTER TABLE app_ref.product_classification
    DROP CONSTRAINT IF EXISTS product_classification_classification_check;
ALTER TABLE app_ref.product_classification
    ADD CONSTRAINT product_classification_classification_check CHECK (classification IN
        ('company_brand', 'branded_competitor', 'generic', 'biosimilar', 'unknown'));

ALTER TABLE app_ref.product_classification
    ADD COLUMN IF NOT EXISTS authority TEXT NOT NULL DEFAULT 'rule'
        CHECK (authority IN ('source', 'mapping', 'none', 'rule'));
ALTER TABLE app_ref.product_classification ALTER COLUMN authority DROP DEFAULT;
ALTER TABLE app_ref.product_classification ADD COLUMN IF NOT EXISTS mapping_ref TEXT;
