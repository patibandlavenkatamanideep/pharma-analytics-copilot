# Data Quality

Everything here was **measured** against the supplied source at commit
`39616c0dc9d3900b4e37fdf3090f50e6770123dd`, not inferred from the documentation.
Each figure is reproduced automatically on every load by the validator in
`app/data/loader.py`, so a future dataset is re-checked rather than assumed to
match.

The governing principle: **the application reports what the data says and
explains where that conflicts with the documentation. It never edits the data,
the generator, or a formula to make an answer look reasonable.**

---

## Summary

| # | Finding | Seed | Full | Severity |
|---|---|---:|---:|---|
| 1 | `market_data` contains no company-brand rows | 0 of 120 | 0 of 1,000,659 | Critical |
| 2 | Market share consequently exceeds 100% | yes | 6 of 7 subcategories | Critical |
| 3 | The two equivalents formulas disagree | 13 of 271 | 602,211 of 2,000,000 | Critical |
| 4 | Geography exists only via ZIP | — | — | Critical |
| 5 | Organizations with no territory mapping | 2 orgs / 32 sales | 0 | High |
| 6 | `period_mo` differs from transaction month | 0 | 186,700 | High |
| 7 | Duplicate organization names | — | 267 groups | High |
| 8 | `mo_offset` exceeds the documented maximum | — | reaches 36 | Medium |
| 9 | Week-ending day contradicts the DDL comment | Sunday | Saturday | Medium |
| 10 | User assignments match no ZIP | 10 of 23 users | 0 | High (seed only) |
| 11 | Competitor ≠ generic | — | — | Medium |

Row counts confirmed on load: **40,000** organizations, **40** products,
**29,728** ZIP mappings, **2,000,000** sales, **23** users. Sources split
898,842 distributor / 100,499 hub / 1,000,659 market data.

---

## 1–2. Market data contains only competitors

**Documented.** `data_source_guide.md` describes `market_data` as "estimated
total market volume across all manufacturers" and says `brand_flag`
"distinguishes NovaPharma products (1) from competitors (0) **within market
data**".

**Measured.** No such row exists. Every `market_data` row has `brand_flag = 0`,
in both datasets.

**Cause, confirmed in the source.** `schema/generate_data.py` selects the
market-data product exclusively from `COMPETITOR_PRODUCTS`. This is not sampling
noise; company products can never appear in that source.

**Consequence.** The documented denominator — "all market_data rows for the
relevant therapeutic area" — is a *competitor-only* total. Dividing our
distributor volume by it therefore overstates share, and for most products
produces an impossible number:

| Subcategory | All-time share |
|---|---:|
| Pemetrexed | 181.6% |
| Gemcitabine | 181.4% |
| Cyclophosphamide | 181.3% |
| Carboplatin | 179.5% |
| Leuprolide | 144.1% |
| Docetaxel | 113.8% |
| Palonosetron | 88.6% |

**What the application does.** Applies the documented formula unchanged, and
reports the ratio together with both components and an explicit warning. A value
above 100% is labelled as inconsistent reported source volumes, not as a share.

**What it deliberately does not do.** It does not add distributor volume into the
denominator, clamp to 100%, switch to a same-source ratio, drop the metric, or
modify the generator. Every one of those would replace a measurement with an
invention, and the resulting number would look reasonable while being fictional.

**Counterpart test.** Because the supplied data cannot exercise the intended
contract, `tests/fixtures/coherent_market.sql` builds a separate small database
in which `market_data` *does* include the company brand. There the formula is
verified end to end: 80 company equivalents over a 200-equivalent market is
exactly 40%. It is kept apart from baseline evaluation so results can never be
confused.

---

## 3. The two equivalents formulas are not interchangeable

**Documented.** `metric_definitions.md` gives
`Equivalents = pack_units × unit_conversion_factor`, then adds
"Alternatively, equivalents can be calculated from milligrams:
`total_mg / mg_equivalent`".

**Measured.** They disagree on **602,211 of 2,000,000** generated rows and
**13 of 271** seed rows.

**Cause.** The generator computes `total_mg = packs × mg_equivalent`. The
"alternative" therefore reduces algebraically to `pack_units`, discarding the
conversion entirely. The two agree only where `unit_conversion_factor = 1.0`.

Worked example — 4 packs of CARBOTREL 150MG (`unit_conversion_factor = 0.333`,
`mg_equivalent = 150`):

```
primary      4 × 0.333   = 1.332 equivalents   ✓
alternative  600 / 150   = 4                   ✗  (this is the pack count)
```

**Decision.** The conversion-factor formula is authoritative and the milligram
form is never used. Supplied factors are preserved exactly, including `0.333` —
not silently corrected to `1/3`. A missing or non-positive factor yields NULL,
never 0 and never 1, and is never silently dropped by `SUM`.

---

## 4–5. Geography resolves only through ZIP

`organizations` has no `territory_name` and no `region_name` column, and the
generator's `ORG_FIELDS` export list omits its internal `_territory_id`. The
territory a facility was *conceived* in is not persisted anywhere the
application can see. The only runtime path is:

```
sales.org_id → organizations.org_id → organizations.zip → zip_territory.zip
```

**A known divergence, deliberately not reconstructed.**
`generate_zip_territories()` keeps the first mapping seen for each ZIP, while
"California North" and "California South" draw from overlapping California ZIP
prefixes. 700 organizations therefore carry an internal intended territory that
differs from their exported mapping. The application uses the exported mapping,
because that is what a real system would have. California is the concrete reason
**state is not a valid substitute for territory**.

**Unmapped ZIPs.** In seed data, `FA011` (60612) and `SA005` (33612) have no
mapping — 32 sales rows. Behaviour differs by role on purpose:

- Director and RAM **fail closed**. A facility whose territory cannot be proven
  is not in scope.
- Exec retains the rows, and the answer labels the unmapped geography. An
  unconditional inner join to `zip_territory` would silently delete them from
  company totals too.

The full dataset has no unmapped organizations, but the behaviour is tested in
the coherent fixture, which includes one deliberately.

---

## 6. Reporting periods follow the week-ending month

**186,700** generated rows carry a `period_mo` different from their
`transaction_date` month, because the generator assigns periods from the
week-ending Saturday while the transaction falls Monday–Friday of that week.

This is a legitimate reporting-calendar convention, not corruption. It is
surfaced rather than "fixed", and it is why named business windows use the
supplied offset columns while only an explicit calendar-day request uses
`transaction_date` — the two are not interchangeable, and a date-range answer
carries a caveat saying so.

---

## 7. Names are labels, not keys

**267** organization-name groups are shared by more than one `org_id` in the
full dataset, including repeated parent names.

`org_hierarchy.md`'s example groups by `COALESCE(grandparent_org_name,
org_name)`, which would merge two genuinely distinct health systems that happen
to share a name. All grouping in this system is by
`COALESCE(grandparent_org_id, org_id)`; names are displayed, never joined on.
A test on the coherent fixture keeps two same-named standalone facilities apart.

---

## 8–9. Range and calendar conventions are read, not assumed

- `period_offsets.md` documents `mo_offset` "up to 35". Generated data reaches
  **36**. The available range is read from the ingestion manifest at load time,
  so nothing is hardcoded.
- The DDL comment and `period_offsets.md` both say `week_ending_date` is a
  Saturday. Generated rows are Saturdays; **all 271 seed rows are Sundays**. The
  loader records the observed convention instead of asserting one.

---

## 10. Seed data cannot exercise role scoping

Found during this review and not noted in the supplied material.

Seed `zip_territory` contains only **9** distinct territory labels —
`Great Lakes, Mid-Atlantic, Mountain, New England, New York Metro, Pacific,
South Central, Southeast, Upper Midwest` — while the `users` table assigns RAMs
to the generator's **15**, including `California North`, `Pacific Northwest`,
`Texas`, `Southeast Atlantic` and `Great Lakes East`.

**10 of 23 users** therefore have an assignment matching zero ZIPs. They
authenticate successfully and every scoped query correctly returns nothing.

This is correct fail-closed behaviour, not a defect — but it means seed-only
testing would give a false sense of security coverage. It is the reason the
README's full-scale mandate is treated as binding for evaluation, and why
`scripts/provision_logins.py --demo` prefers users whose assignment actually
resolves in the loaded dataset.

---

## 11. Competitor is not the same as generic

`brand_flag = 0` means "competitor/generic" per the DDL comment, and covers
branded competitors: TAXOTERE, GEMZAR, ALIMTA, KEYTRUDA and AVASTIN all carry
`brand_flag = 0`. There is no generic or biosimilar column anywhere in the
supplied schema.

A question like "what is the generic share in Platinum Compounds" therefore
cannot be answered from `brand_flag` alone without relabelling all competitor
volume as generic. `migrations/003_derived_classification.sql` adds an explicit,
versioned lookup derived by a documented drug-name rule:

| Rule | Classification |
|---|---|
| `brand_flag = 1` | `company_brand` |
| name ends `' BIOSIMILAR'` | `biosimilar` |
| name ends `' GENERIC'` | `generic` |
| otherwise | `branded_competitor` |

It is marked derived-not-supplied, versioned with the metric registry, and
answers that use it say the classification is derived.

---

## Reproducing these figures

```bash
python3 schema/generate_data.py
python3 scripts/load_data.py --mode full --json
```

The manifest written to `app_meta.dataset_manifest` records source hashes, row
counts, the reporting anchor, source coverage and every warning above, so any
result can be traced to the exact dataset that produced it.
