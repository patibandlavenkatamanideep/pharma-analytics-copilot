# Assumptions & Decisions

Baseline: upstream `cveeraiy/nl2sql-assignment` @ `39616c0dc9d3900b4e37fdf3090f50e6770123dd`.
All supplied files under `docs/` (original eight), `schema/create_tables.sql`, `schema/seed_data.sql`
and `schema/generate_data.py` are preserved unmodified. Everything this project adds is additive.

Each entry records the decision, the evidence behind it, confidence, the alternative rejected,
and how it is verified.

---

## A1 — Database: PostgreSQL 16, original logical schema preserved

**Decision.** PostgreSQL 16. The five supplied tables keep their exact names, columns, types and
keys. `schema/create_tables.sql` is untouched; a separate additive migration
(`migrations/001_base_schema.sql`) expresses the same logical schema in PostgreSQL dialect.

**Evidence.** The supplied DDL is annotated `-- Target: SQLite`. README explicitly permits "any SQL
database … SQLite, PostgreSQL, MySQL". The dialect differences required are narrow and mechanical:
`INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGSERIAL`, `REAL` → `DOUBLE PRECISION`, `TEXT` unchanged.

**Why not SQLite.** Role scoping here is a security requirement, not a display filter. PostgreSQL
gives database-enforced row-level security and per-column privileges, so a compiler bug cannot by
itself leak another territory's rows or WAC. SQLite has no database-level principals; the entire
boundary would live in application code.

**Confidence.** High on the permission to choose; high on the dialect mapping being faithful.

**Verification.** `tests/test_schema_parity.py` asserts the migration's table/column names and
nullability match `schema/create_tables.sql` exactly, so drift fails the build.

---

## A2 — Equivalents use `unit_conversion_factor`, never the milligram formula

**Decision.** `equivalents = pack_units * products.unit_conversion_factor`, joined on NDC.
The documented alternative `total_mg / mg_equivalent` is **not** used anywhere.

**Evidence (measured, not inferred).** The two formulas disagree on **13 of 271** seed rows and
**602,211 of 2,000,000** generated rows. The cause is in the generator itself:
`generate_data.py:576` computes `total_mg = packs * mg_equiv`. Therefore
`total_mg / mg_equivalent` algebraically reduces to `pack_units`, discarding the conversion entirely.
It coincides with the primary formula only where `unit_conversion_factor = 1.0`.

Worked case: 4 packs of CARBOTREL 150MG, `unit_conversion_factor = 0.333`, `mg_equivalent = 150`.
Primary → `4 * 0.333 = 1.332` equivalents. Alternative → `600 / 150 = 4`. The alternative returns
pack counts mislabelled as equivalents.

**Rejected.** Averaging the two, or preferring the milligram form for rows with a NULL factor.

**Confidence.** High. `metric_definitions.md` presents the primary formula first and normatively
("Equivalents = pack_units × unit_conversion_factor"); the milligram form is introduced as
"Alternatively". Where a stated alternative is arithmetically inconsistent with the primary, the
primary governs.

**Handling.** Supplied factors are preserved exactly, including `0.333` — not silently corrected to
`1/3`. A NULL or non-positive factor makes the row's equivalents NULL and raises a data-quality
warning; it is never coerced to 0 or 1, and never silently dropped by `SUM`.

---

## A3 — Market share keeps the documented contract; impossible ratios are reported, not repaired

**Decision.** Numerator = distributor equivalents where `brand_flag = 1`. Denominator = **all**
`market_data` equivalents in the matching `market_subcategory`. Identical population, scope and
period filters apply to both components. Zero denominator → NULL, never 0 and never infinity.

**Evidence.** In both supplied datasets every `market_data` row has `brand_flag = 0`.
This is not a sampling artifact: `generate_data.py:604-610` selects the market-data product
exclusively from `COMPETITOR_PRODUCTS`. The documentation
(`data_source_guide.md`) states market data "Includes both branded and generic products from all
competitors" and that `brand_flag` "distinguishes NovaPharma products (1) from competitors (0)
within market data" — no such rows exist.

Consequence: the denominator is a competitor-only total, not a total market. Applying the documented
formula to full generated data yields **Docetaxel 113.78%** and **Carboplatin 179.54%**. Six of the
seven NovaPharma subcategories exceed 100%.

**Decision detail.** We do **not** add distributor volume into the denominator, clamp to 100%,
switch to a same-source ratio, or patch the generator. Any of those would silently invent a number.
Instead the answer reports the ratio, its two components, and an explicit data-quality warning; a
ratio above 100% is labelled as inconsistent reported source volumes rather than a reliable share.

**Reading the apparent contradiction.** `metric_definitions.md` says sources "should never be mixed
in a single ratio calculation" while its own formula divides distributor by market data. Read
literally the formula would be self-prohibiting. We read it as forbidding *contamination within a
component* (e.g. summing distributor and market rows into one side). `data_source_guide.md` settles
it: "Market share always uses `distributor` in the numerator and `market_data` in the denominator."

**Confidence.** High that this is the documented contract; high that the supplied data cannot satisfy
its stated meaning.

**Verification.** A separately labelled coherent fixture (`tests/fixtures/coherent_market.sql`)
contains NovaPharma rows inside `market_data` and tests the intended formula end to end
(80 / 200 = 40%). It is kept strictly apart from baseline-data evaluation.

---

## A4 — Geography resolves through ZIP only

**Decision.** Scope path is `sales.org_id → organizations.org_id → organizations.zip →
zip_territory.zip`. `zip_territory` is the sole runtime authority for territory and region.

**Evidence.** `organizations` has no `territory_name` or `region_name` column in the supplied DDL,
and `generate_data.py`'s `ORG_FIELDS` export list omits the generator's internal `_territory_id`.
The territory a facility was *conceived* in is not persisted anywhere the application can see.

**Known divergence, deliberately not reconstructed.** `generate_zip_territories()` keeps the first
mapping seen for a ZIP (`if zipcode in zip_terr: continue`), while T013 "California North" and T014
"California South" draw from overlapping California ZIP prefixes. 700 organizations therefore carry
an internal intended territory different from their exported ZIP mapping. The application uses the
exported mapping and does not attempt to recover generator intent — the mapping is what a real
system would have. California is the concrete reason state is not a valid substitute for territory.

**Unmapped ZIPs.** Seed `FA011` (60612, 30 sales) and `SA005` (33612, 2 sales) have no
`zip_territory` row — 32 sales total. Behaviour differs by role and is deliberate:
Director/RAM **fail closed** (an unmappable facility cannot be proven in scope);
Exec retains the rows in company totals and the answer labels the unmapped geography. An
unconditional inner join to `zip_territory` would wrongly delete these rows from Exec totals too.

**Confidence.** High.

---

## A5 — Seed data cannot exercise role scoping; full data is the real target

**Decision.** `seed` and `full` are mutually exclusive load modes. Correctness and security
evaluation run against **full** data. Seed is a development fixture only.

**Evidence (found in this review, beyond the supplied notes).** Seed `zip_territory` contains only
9 distinct territory labels — `Great Lakes, Mid-Atlantic, Mountain, New England, New York Metro,
Pacific, South Central, Southeast, Upper Midwest` — while the `users` table assigns RAMs to the
generator's 15 labels, including `California North`, `California South`, `Pacific Northwest`,
`Texas`, `Southeast Atlantic`, `Great Lakes East`. Most RAM logins therefore match **zero** ZIPs in
seed mode.

**Consequence.** An empty result for a RAM under seed data is correct fail-closed behaviour, not an
authorization defect — and seed-only testing would give a false sense of security coverage. This is
an additional reason the README's full-scale mandate is treated as binding rather than aspirational.

---

## A6 — Reporting periods are refresh-relative, never wall-clock

**Decision.** Named business windows use the supplied offset columns. Explicit calendar quarters and
years use the `period_*` label columns. Only an explicit calendar-day request uses
`transaction_date`. The anchor comes from a validated ingestion manifest, never `CURRENT_DATE`.

**Mapping.**

| Term | Filter | Source |
|---|---|---|
| R3M / last 3 months | `mo_offset IN (0,1,2)` | `metric_definitions.md`, `period_offsets.md` |
| R6M (business prior period) | `mo_offset IN (3,4,5)` | `metric_definitions.md` — the *preceding* 3 months |
| literal "last six months" | `mo_offset BETWEEN 0 AND 5` | `period_offsets.md` example |
| last month | `mo_offset = 1` | `period_offsets.md` |
| last quarter | `mo_offset IN (1,2,3)` | `period_offsets.md` — **not** the previous calendar quarter |
| R30D / last 4 weeks | `wk_offset <= 3` | `period_offsets.md` |

**Ambiguities surfaced rather than guessed.** "R6M" reads naturally as six months but is defined as
a 3-month prior window; "last quarter" as offsets 1-3 at a September anchor selects June-August,
whereas calendar Q2 is April-June. Where the distinction changes the number, the assistant states the
interpretation and the resolved date range in the answer; for a genuinely ambiguous "last quarter"
against an explicit calendar quarter it asks.

**Reporting calendar.** Periods are assigned by **week-ending month**
(`generate_data.py`: `period_mo = sat.strftime("%Y-%m")` while the transaction date falls Mon-Fri of
that week). **186,700 generated rows** have a `period_mo` different from their `transaction_date`
month. This is a legitimate reporting-calendar convention, not corruption, and is surfaced rather
than "fixed". It is also why mixing offset filters with date filters in one window is rejected.

**Not hardcoded.** Generated data reaches `mo_offset = 36`, beyond the documented maximum of 35.
The available range is read from the manifest at load time.

**Week-ending day.** The DDL comments and `period_offsets.md` both say Saturday. Generated weeks do
end Saturday; all 271 seed rows end **Sunday**. Ingestion records the observed convention instead of
asserting one.

---

## A7 — Accounts are identified by ID; names are labels

**Decision.** Top-level account identity is `COALESCE(grandparent_org_id, org_id)`, displayed with
the corresponding name. All grouping is by ID.

**Evidence.** Full data contains **267 repeated organization-name groups**, including repeated
parent names. `org_hierarchy.md`'s example groups by `COALESCE(grandparent_org_name, org_name)`,
which would merge two genuinely distinct health systems that happen to share a name.

**Scope ordering (security-critical).** Territory/region scope is applied at the **facility** row
before hierarchy rollup. A grandparent being visible must never pull in its facilities in other
territories. Aggregates for a partially visible system are therefore partial by construction, and
the answer says so.

---

## A8 — WAC is a transaction amount, Exec-only, and restricted in every SQL clause

**Decision.** `Gross Revenue = SUM(sales.wac)` over distributor rows with `brand_flag = 1`.
It is never multiplied by volume — `generate_data.py:579` sets
`wac = total_mg * wac_per_mg * noise`, already a per-transaction dollar amount.
It is list-price revenue, never described as net, rebated or profit.

**Authorization.** Permitted only when `role = 'exec'` AND `can_view_wac = 1` (both, not either).
For Director/RAM the column may not appear in SELECT, WHERE, ORDER BY, GROUP BY, HAVING, a CTE, a
subquery, a window expression, an alias, a wildcard or whole-row serialization. A request to *rank
by* revenue while hiding the column is refused, because the ordering itself discloses it; a labelled
volume alternative is offered instead and is never called "revenue".

**Enforcement is layered**, so no single bug is sufficient: the plan schema cannot express a WAC
metric for a non-Exec principal; the compiler will not emit the identifier; the AST validator
rejects it; and the PostgreSQL runtime role holds no `SELECT` privilege on `sales.wac` at all.

---

## A9 — Competitor ≠ generic

**Decision.** "Generic share" is not answerable from `brand_flag` alone and is not silently
approximated.

**Evidence.** `brand_flag = 0` means "competitor/generic" per the DDL comment and covers branded
competitors — TAXOTERE, GEMZAR, ALIMTA, KEYTRUDA, AVASTIN are all `brand_flag = 0`. There is no
generic/biosimilar classification column.

**Handling.** A curated additive lookup (`migrations/003_derived_classification.sql`) classifies the
40 supplied products by drug name (`% GENERIC` → generic, `% BIOSIMILAR` → biosimilar, else branded).
It is clearly marked derived-not-supplied, is versioned with the metric registry, and the answer
discloses that the classification is derived. Without it the request is refused as unsupported
rather than answered with competitor volume relabelled as generic.

---

## A10 — 340B filtering is facility-level by default

**Decision.** `is_340b` is an organization attribute, so "exclude 340B" excludes the contributions of
qualifying **facilities**. A health system may contain both 340B and non-340B facilities.

**Handling.** The default is applied and disclosed in the answer. Where the user's intent could
plausibly be "exclude any system containing a 340B facility" and the two readings differ materially
for the requested population, the assistant asks instead of guessing.

---

## A11 — Semantic distinctions the system must not blur

- No observations ≠ a verified zero. Absent source coverage is reported as unavailable.
- Percentage-point change ≠ percent growth. Share deltas are points; volume growth is a ratio.
- Growth with a zero prior period is "new activity", not infinite or 100%.
- Historical transactions ≠ currently active accounts. `org_status = 'Active'` is applied only when
  the question asks for active organizations.
- Pack units are a commercial volume measure; equivalents normalize package sizes within the supplied
  system. Neither is clinical dose equivalence across therapies.
- Organization counts specify the counted entity and use `COUNT(DISTINCT id)`; hierarchy levels are
  never summed as if all rows were facilities.
- Ratios aggregate as `SUM(numerator) / SUM(denominator)`, never as a mean of per-row percentages.
  Independent sources are pre-aggregated to a common grain before joining, to avoid multiplicative
  fan-out.

---

## A12 — Weighted share trend is proposed, not established

`metric_definitions.md` gives `Weighted Trend = (R3M Share - R6M Share) × (R3M Volume + R6M Volume)`
but does not say whether "Volume" is NovaPharma or total-market volume. We implement **total market
equivalents**, label it a proposed definition in both the registry and the answer, and keep the
metric optional. It is not presented as established business truth.

---

## A13 — Authentication is added; the supplied `users` table is not modified

**Decision.** `users` keeps its exact supplied shape and remains the authority for role, assignment
and `can_view_wac`. Credentials and sessions live in new tables (`app_auth.credentials`,
`app_auth.sessions`) in a separate schema, unreachable from analytical SQL.

**Rationale.** `users` has no password, hash or secret column, so an email alone is an identifier,
not proof of identity. A role dropdown or email-only form would let any visitor become an Exec,
which would not demonstrate access control at all.

**Implementation.** Argon2id password hashing, server-side opaque session tokens in an
HttpOnly/Secure/SameSite cookie. The browser never supplies role, user_id or scope; those are loaded
server-side from `users` on every request. Evaluator logins are provisioned by a script from
environment variables and never committed.

---

## A14 — Open items pending external access

| Item | State | Effect |
|---|---|---|
| AWS credentials | `sts get-caller-identity` → `InvalidClientTokenId` (static keys dated 2026-01-02) | Blocks Bedrock and RDS/App Runner deployment |
| Bedrock model access | Unverified — depends on the above | Live NL accuracy unmeasured until resolved |
| Assessment assistance rules | Not yet confirmed by the candidate | Attribution recorded in commits regardless |

Work proceeds locally against PostgreSQL 16 with a deterministic offline planner so that every
non-LLM layer is testable now. Nothing in this document claims a deployment or live measurement that
has not occurred.
