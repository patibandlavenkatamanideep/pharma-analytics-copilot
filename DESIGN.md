# DESIGN

A natural-language analytics assistant over a pharmaceutical sales database,
built for the NL-to-SQL assessment. This document explains what was built, why,
what is verified, and what is not.

Everything stated as measured here was measured on the full 2,000,000-row
dataset unless it says otherwise. Claims about deployment and live-model
accuracy are marked explicitly, because at the time of writing they are not yet
true — see [Status](#12-status-and-what-is-not-yet-proven).

---

## 1. The shape of the problem

The assignment looks like "turn English into SQL". The hard part is not the SQL.
It is that **the same question must produce a different, correct answer for
different people**, that several of the supplied business rules are subtly
counter-intuitive, and that the supplied data does not satisfy its own
documentation. A system that generates plausible SQL and executes it will be
confidently wrong on all three counts.

So the design question was: where does correctness live? Three candidate
answers, and why two were rejected.

| Approach | Why not |
|---|---|
| Model writes SQL, app filters results | Row scope becomes a post-filter, so the model can leak by aggregating. "Hide the revenue column but sort by it" still discloses revenue. |
| Model writes SQL, app validates it | Better, but the validator must anticipate every unsafe construct. A validator is a good *second* line; as the only line it fails open on anything unanticipated. |
| **Model picks from a closed vocabulary; the server compiles the SQL** | The model cannot express an unsafe query, because the plan type has no way to say it. Chosen. |

The result is closer to a governed semantic layer with a natural-language front
end than to a text-to-SQL translator.

---

## 2. Architecture

```
Browser (React, same origin)
   │  opaque session cookie — no role, no user id, no scope
   ▼
FastAPI
   │
   ├─ 1. identity        verified session → Principal, re-read from `users` every request
   ├─ 2. conversation    owner-scoped structured state (typed plan + resolved ids)
   ├─ 3. vocabulary      entity names, already narrowed to the principal's scope
   ├─ 4. PLAN            ← the only step the model touches
   ├─ 5. authorize       policy check against the Principal
   ├─ 6. compile         server-owned metric SQL, allowlisted identifiers, bound values
   ├─ 7. validate        full AST parse of the final statement
   ├─ 8. execute         read-only, time-bounded, scope-bound transaction
   ├─ 9. render          numbers formatted from actual rows; data-quality findings
   └─ 10. audit          hashes, counts, timings — never values, rows or prompts
   ▼
PostgreSQL 16
   ├─ RLS on organizations and sales      → row scope
   └─ per-column grants on sales.wac      → pricing access
```

The ordering is the design. The plan is authorized *before* it is compiled, the
SQL is validated *before* it is executed, and it executes on a connection whose
privileges were fixed *before* the model ran. No step can be skipped by anything
the model or the user says.

---

## 3. Database choice: PostgreSQL 16

**Rationale.** Role scoping here is a security requirement, not a display
filter. PostgreSQL gives database-enforced row-level security and per-column
privileges, so a bug in the compiler cannot by itself leak another territory's
rows or WAC. SQLite has no database-level principals; the entire boundary would
live in application code, and the strongest claim available would be "our code
is careful".

The README explicitly permits any SQL database.

**Schema fidelity.** The supplied `schema/create_tables.sql` is untouched.
`migrations/001_base_schema.sql` expresses the same logical schema in PostgreSQL
dialect and changes **dialect only**:

| SQLite | PostgreSQL | Why |
|---|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGSERIAL PRIMARY KEY` | no SQLite rowid alias |
| `REAL` | `DOUBLE PRECISION` | same IEEE-754 double |
| `TEXT`, `INTEGER` | unchanged | — |

Table names, column names, column order, nullability, keys and foreign keys are
identical. Everything else the project adds is additive: new schemas
(`app_auth`, `app_conv`, `app_meta`, `app_ref`), indexes, and security policies.

---

## 4. Security

This is the part worth reading closely, because it is where the assignment is
most easily failed in a way that still demos well.

### 4.1 Identity

The supplied `users` table has **no password, hash or secret column**. An email
address therefore identifies a user; it does not authenticate one. A role
dropdown or an email-only form would let any visitor become an Exec, which would
demonstrate nothing.

- Credentials and sessions live in `app_auth`, joined to `users` by `user_id`.
  `users` itself is never modified.
- argon2id password hashing. The cookie carries an opaque random token; only its
  SHA-256 is stored, so a dump of the sessions table cannot be replayed.
- **Role, territory, region and `can_view_wac` are re-read from `users` on every
  request**, never cached in the session and never accepted from the client. A
  scope change takes effect immediately.
- Every login failure returns one identical message and performs comparable
  work, so the response does not disclose whether an address is registered.
- A correct password is not sufficient: the account must also resolve to a
  usable scope, so an unassigned Director or RAM is refused at login.

### 4.2 Row scope — two orthogonal controls

**Which pool** decides **column** access. **Which GUC** decides **row** access.

```
Exec pool    → login role granted SELECT on sales including wac
Scoped pool  → login role never granted sales.wac at all
```

Each login role is a member of exactly one privilege role, so a scoped
connection has **no membership path** to the Exec role — `SET ROLE` cannot
escalate it. Pricing access is a property of the TCP connection, not a branch in
application code.

Row scope is bound per transaction:

```sql
set_config('app.scope_kind',  'territory', true)   -- is_local => discarded at COMMIT
set_config('app.scope_value', 'Texas',     true)
```

`set_config` is used rather than `SET LOCAL` because it is the *parameterized*
form — no value is ever interpolated into SQL text. A custom GUC is not
self-authenticating, so generated SQL may never call `set_config`, `SET`, or any
role-changing statement; only compiler-owned single SELECTs are executed.

`current_setting(..., true)` returns NULL when unset, so **every policy
comparison is false by default and the default is DENY**. An unauthenticated or
misconfigured connection sees zero rows rather than everything.

### 4.3 Geography resolves only through ZIP

`organizations` has **no** `territory_name` or `region_name` column, and the
generator never exports its internal territory assignment. The only runtime path
is:

```
sales.org_id → organizations.org_id → organizations.zip → zip_territory.zip
```

The `sales` policy is deliberately expressed in terms of `organizations`:

```sql
EXISTS (SELECT 1 FROM organizations o WHERE o.org_id = sales.org_id)
```

so the organizations policy applies transitively. This is what makes scope apply
at the **facility row before any hierarchy rollup** — a visible grandparent
cannot pull in its facilities in other territories, because the rollup only ever
aggregates rows that already survived the policy. The coherent fixture tests
exactly this: a RAM in New York Metro sees 90 of Acme Health System's packs, not
the 110 an Exec sees, because Acme's Dallas facility is out of scope.

### 4.4 WAC is layered, four deep

| Layer | What stops it |
|---|---|
| Plan schema | `wac_revenue` is a metric key; ranking is always by the plan's own metric, so "sort by revenue, hide the column" is not expressible |
| Policy | `requires_wac` is computed transitively; needs `role = 'exec'` **AND** `can_view_wac = 1` |
| Validator | rejects `wac` in any clause, and a wildcard that could resolve against `sales` |
| **Database** | the scoped role holds no privilege on the column |

Each layer is independently sufficient. Verified: a scoped connection is refused
`wac` through 12 different clauses — select, alias, where, order by, group by,
having, subquery, CTE, window, wildcard, derived expression and CASE — by
PostgreSQL itself.

A refused pricing request always offers a labelled volume alternative. Volume is
never called revenue.

### 4.5 Known limitation, stated plainly

PostgreSQL grants `pg_catalog` and `information_schema` to PUBLIC by design, and
revoking that breaks ordinary client operation. Those catalogs hold no business
data — no sales, no organizations, no pricing, no identity rows — but they do
expose object and role *names*. Reaching them requires sending arbitrary SQL,
and there is no path to do so: the compiler emits only server-owned statements,
the validator rejects both schemas by name, and no endpoint accepts SQL text.
**These are blocked by the application layer, not by the database.** A test
asserts the validator refuses them; the stronger claim would have been untrue.

---

## 5. Domain knowledge

### 5.1 How it is integrated

A versioned registry (`app/analytics/metrics.yaml`, v1.0.0) holds 13 metrics.
Each entry anchors to the supplied document that defines it, so any number can
be traced back to the specification.

**The essential rules are in every planning request, not retrieved.** A
restriction that only arrives when some document happens to be retrieved is not
a restriction. The registry summary and the pricing rule are always present;
retrieval would only ever be an optimisation on top.

The model chooses a metric *key*. It never supplies a formula, a table, a join
or a fragment. The arithmetic is the server's.

### 5.2 The three rules most likely to be got wrong

**Equivalents.** `pack_units × unit_conversion_factor`. The documented
alternative `total_mg / mg_equivalent` is **never** used: the generator sets
`total_mg = packs × mg_equivalent`, so that formula algebraically reduces to
`pack_units` and discards the conversion entirely. Measured: the two disagree on
**602,211 of 2,000,000 rows**. Four packs of CARBOTREL 150MG (factor 0.333) is
1.332 equivalents, not 4.

**Market share.** Numerator is distributor volume, `brand_flag = 1`. Denominator
is **all** `market_data` rows in the matching `market_subcategory` — not narrowed
to our drug name, not restricted to `brand_flag = 1`. Grouping by product bridges
to subcategory, so ZENOVAX is measured against the Docetaxel market rather than
against itself.

**Periods.** "R6M" is defined as the three months *preceding* R3M (offsets 3-5),
not six months; a literal "last six months" is offsets 0-5. "Last quarter" is
offsets 1-3, which at a September anchor means June–August, not calendar Q2.
Windows are anchored to the dataset's own reporting anchor, never the wall clock.

### 5.3 Where the data contradicts the documentation

Measured on load, reproduced independently by the loader's validator:

| Finding | Measured | Handling |
|---|---:|---|
| `market_data` holds only competitor rows | 0 company rows of 1,000,659 | Report the ratio with both components and a warning. Never clamp, never add the numerator into the denominator, never patch the generator. |
| Impossible market shares | Docetaxel 113.78%, Carboplatin 179.54% | Labelled as inconsistent reported source volumes, not as a share. |
| Equivalents formulas disagree | 602,211 rows | Conversion factor is authoritative. |
| `period_mo` ≠ transaction month | 186,700 rows | A reporting-calendar convention (periods follow week-ending month), surfaced not "fixed". |
| Duplicate organization names | 267 name groups | All grouping is by stable id; names are labels. |
| `mo_offset` exceeds documented max of 35 | reaches 36 | Range read from the manifest, never hardcoded. |
| Week-ending day | Saturday in generated, **Sunday** in all 271 seed rows | Observed convention recorded rather than asserted. |
| Seed territories vs user assignments | 9 labels vs 15 referenced | Most RAM logins resolve to zero rows under seed; full data is the only coherent target. |

The last row is why **full data is treated as the only valid target for security
evaluation**: seed-only testing would give a false sense of coverage.

---

## 6. The plan type

```python
metric        : one of 13 keys
dimensions    : ≤ 4 from a closed enum
filters       : product / market / account / GPO / archetype / 340B / status
time          : named window | month offsets | week offsets | period labels | dates
comparison    : optional second window
ranking       : direction + bounded limit, always on the plan's own metric
clarification : set instead of answering, when the question is ambiguous
```

`extra="forbid"`, every enum closed, every limit bounded. There is **no field**
for a role, a user id, a scope, a table, a column or SQL. A test asserts their
absence structurally, so it cannot regress silently.

Ranking deliberately cannot take a separate sort metric: ordering by revenue
discloses revenue even when the column is hidden.

---

## 7. Compiler and validator

**Compiler.** Every identifier comes from a closed allowlist in the source;
every value is a bound parameter. Parameterization cannot protect an identifier,
so identifiers are simply never variable.

Ratios aggregate as `SUM(numerator) / SUM(denominator)` from separately
pre-aggregated CTEs — never the mean of per-row percentages, and never a
raw-sales join that could multiply rows. A zero denominator yields NULL, never 0
and never a division error.

**Validator.** Parses the complete final statement into an AST with sqlglot and
walks it. No regex: a comment, a string literal or unusual whitespace defeats
regex matching. It runs on the final text, after every rewrite, and that same
text is what executes.

It enforces a **strict function allowlist**. A denylist was tried first and was
the wrong shape — sqlglot models hundreds of functions as their own node
classes, so `generate_series` arrived as `ExplodingGenerateSeries` and slipped
past a list of forbidden names. Measured across every metric shape, the compiler
emits exactly five functions (`SUM`, `COUNT`, `COALESCE`, `NULLIF`, `CAST`), so
the allowlist is tiny and anything unrecognised is refused.

Also blocked: multiple statements, writes, DDL, data-modifying CTEs, recursive
CTEs, catalog and auth schemas, and non-allowlisted relations.

---

## 8. LLM provider and prompt design

> The AI layer has its own document: [`docs/AI_SYSTEM_DESIGN.md`](docs/AI_SYSTEM_DESIGN.md)
> covers the plan-type interface, prompt architecture, structured output,
> failure modes, the injection threat model and how the AI layer is evaluated.
> This section is the summary.

**Provider: AWS Bedrock**, one provider, one model, configurable. The adapter
uses the Anthropic SDK's Bedrock client with a forced `emit_plan` tool call, so
the response is a typed object rather than prose to be parsed. One bounded
repair attempt is made if the plan fails validation; the model is told exactly
what failed and cannot widen the schema, only satisfy it.

**The prompt carries the rules, not the schema.** It contains the metric
registry summary, the window definitions with their counter-intuitive
semantics spelled out, the pricing restriction when it applies, and the entity
vocabularies already narrowed to the principal's scope. It contains no table
names and no SQL.

**A deterministic offline planner** (`OfflinePlanner`) implements the same
interface with keyword rules. It exists so the compiler, authorization,
execution and rendering layers stay testable and demonstrable with no
credentials and no spend, and so CI failures mean the pipeline broke rather than
that a model's wording drifted. It is labelled throughout as not a language
model, and it is never presented as measuring NL accuracy.

**Model availability note.** This AWS account cannot invoke the Claude 5 family
(`AccessDeniedException: not available for this account`). Opus 4.5, Sonnet 4.5
and Haiku 4.5 are listed and reachable, so the configured model is Opus 4.5.

---

## 9. Conversation

Persisted state is the **typed plan and the resolved entity ids** — never SQL,
never result rows. A follow-up patches a structured object rather than
re-reading old prose, so old text can never overwrite who the user is.

- "Break that down by quarter" adds a grain and keeps the population.
- "Those five accounts" freezes the resolved ids and drops the ranking, so the
  cohort is not silently re-ranked.
- "Exclude 340B" changes one filter and nothing else.
- Every read is filtered by `owner_user_id`; another user's conversation returns
  the same "does not exist" as a missing one.
- A conversation records the scope it began under. If the owner's role or
  assignment changes, carried state is discarded rather than reused under
  different permissions.

---

## 10. Performance

Measured on the full dataset, local PostgreSQL 16.

Indexes are workload-driven and were revised by measurement, not guessed. The
first attempt used a bare `(ndc)` index on `sales`; `EXPLAIN (ANALYZE, BUFFERS)`
showed it probing 52,039 rows per NDC and discarding 50,720 of them in the heap
— 120,468 heap blocks, **3,304 ms** for a single Docetaxel denominator. Moving
source and reporting offset into the index and covering `pack_units` turns it
into an index-only scan at **6.8 ms**, a 485× improvement.

Representative end-to-end pipeline timings (offline planner, so these are
database plus compile plus render, excluding model latency):

| Question | DB | Total |
|---|---:|---:|
| Zenovax market share, R3M | 15 ms | 45 ms |
| Top 5 accounts by pack units, R3M | 231 ms | 320 ms |
| WAC revenue by product, last month | 36 ms | 62 ms |
| Exclude 340B, top 5 accounts | 423 ms | 453 ms |
| Market share by territory | 54 ms | 74 ms |
| Weighted share trend by account | — | 870 ms |

Bounds rather than hopes: read-only transactions, a 5 s statement timeout, an
idle-in-transaction timeout, and a result cap. A `LIMIT` does **not** bound the
cost of the aggregation beneath it, which is why the timeout is the real
defence; a test confirms a runaway cross join is cancelled.

The result cap fetches `cap + 1` rows so a truncated answer is distinguishable
from one that merely fills the cap — with `LIMIT` equal to the cap they are
identical and truncation is silently reported as a complete answer.

Under load, ten question shapes measured five times each
(`scripts/benchmark.py`):

| Concurrency | Overall p50 | Overall p95 | Throughput | Errors |
|---:|---:|---:|---:|---:|
| 1 | 96 ms | 450 ms | 7.5 req/s | 0 / 50 |
| 8 | 138 ms | 764 ms | 32.2 req/s | 0 / 50 |

8× the concurrency gives 4.3× the throughput with latency roughly doubling and
no timeouts — the per-role pool of 8 connections is the limiting factor, as
intended. These exclude model latency by design, so it is clear which layer
costs what.

**No result cache.** Not measured as necessary. If one is added, its key must
include principal, scope, pricing permission, normalized plan, dataset version,
policy version and metric version.

---

## 11. Testing

148 tests, all passing, in four layers that deliberately do different jobs.

| Layer | Count | What it proves |
|---|---:|---|
| Unit | 41 | Window semantics, plan-schema limits, registry invariants, formatting |
| Integration | 16 | Metric correctness against 2M rows, vs **hand-written reference SQL** |
| Coherent fixture | 14 | The intended market-share contract, on a separate hand-calculated database |
| Security | 63 | The authorization boundary, from three directions |
| Failure paths | 14 | Provider outage, query timeout, database down, empty results, oversized input, schema mismatch |

**Expected values never come from the compiler under test.** Integration
expectations are SQL written by hand in the test files; if the compiler and the
reference disagree, one is wrong and the test says so. Using the compiler to
generate its own expectation would make the suite agree with any bug.

The **coherent fixture** is a separate small database modelling a market the
supplied data cannot: one where `market_data` includes the company's own volume.
Every figure is hand calculated in the fixture header — 80 company equivalents
over a 200-equivalent market is exactly 40%. It is kept strictly apart from
baseline evaluation so the two can never be confused.

Metamorphic properties tested: free drug moves neither paid demand nor share;
340B include = exclude + only; `r3m + r6m_prior = last_6_months`; a Director's
total equals the sum of the RAM totals inside the region; renaming does not
merge distinct ids; out-of-scope data cannot change a RAM's result.

Beyond the suite, `evals/questions.yaml` holds 38 **held-out** checks across 15
families, run by `scripts/run_evals.py`. The planner prompt carries metric
definitions and window semantics, not these phrasings; several are deliberate
paraphrases and the compositional family asks for combinations found in no
document. It scored 30/38 on its first run and 38/38 after the fixes below.

CI (`.github/workflows/ci.yml`) runs the whole no-spend path on every push:
bootstrap, the **full** dataset, the coherent fixture, a startup assertion that
the security boundary is intact, the security gate, all 148 tests and the
held-out set. Full data rather than seed on purpose — under seed most scoped
accounts resolve to nothing and the security tests would pass vacuously.

### Bugs these tests found

Worth listing, because each would have produced a confidently wrong answer:

1. **`ION` matched inside `reg·ion·`.** Entity matching used substrings, so "How
   are territories in my region performing" silently filtered a Director to
   ION-affiliated accounts and under-reported their region by ~80%. All
   vocabulary matching is now word-boundary anchored.
2. **Market share by product returned NULL for everything**, because the
   denominator grouped by our own drug name collapsed "the market" to our own
   product. Now bridged to subcategory.
3. **Follow-ups rebuilt the plan from scratch**, so "break that down by quarter"
   dropped the account population and "exclude 340B" discarded the top-5 ranking
   and returned every account.
4. **Truncation was invisible** (§10).
5. **A market we do not compete in returned nothing** instead of the real market.
6. **`generate_series` slipped past the validator's denylist** (§7).
7. **Counting metrics counted the wrong population.** `account_count` had no
   source filter, so organizations appearing only in third-party market data
   were counted as accounts we sell to — 8,916 instead of 7,116, a 25%
   overstatement with no visible symptom.
8. **A fresh question inherited an earlier turn's filters.** After "exclude
   340B", an unrelated question three turns later silently kept the exclusion
   and returned a total 9% below the truth. Filters now carry only on a detected
   follow-up, and carrying is disclosed in the answer.
9. **An out-of-scope territory was answered instead of refused.** A RAM asking
   for Texas received their own territory's numbers — no leak, but an answer to
   a question they did not ask.

---

## 12. Status and what is not yet proven

Stated plainly rather than implied.

**Verified**
- Full 2M-row dataset loads in 78 s and every documented anomaly is reproduced
  automatically by the loader's validator.
- The authorization boundary holds on full data across 63 tests.
- Metric semantics match hand-written reference SQL.
- The API and UI serve together from one origin; forged cookies and forged role
  headers are refused.

**Not yet true at the time of writing**
- **No cloud deployment.** The public URL deliverable is outstanding.
- **No live-model accuracy measurement.** Bedrock requires an Anthropic use-case
  details form to be submitted for this account; until then the live planner
  cannot be exercised and no accuracy number exists. The offline planner is not
  a substitute and no figure from it is presented as NL accuracy.
- Cold-start and cost have not been measured. Concurrency has (§10).

**Operational limitations**
- Single-host deployment has no availability guarantee.
- The schema contract is fixed and **enforced**. New rows, names, values,
  periods and combinations within the supplied schema are supported; a
  different schema is refused at load time by `app/data/schema_contract.py`,
  naming the exact tables, columns and types that differ. Additive columns are
  compatible and do not change the fingerprint.
- The offline planner is a keyword matcher and will mis-read phrasings the
  live model would handle.

---

## 13. Trade-offs, and what more time would buy

**Deliberately not built:** result caching (unmeasured need), vector retrieval
(the corpus is eight documents — putting the rules in every prompt is both
cheaper and safer), multi-provider routing, an agent loop (no open-ended
exploration is required), charts.

**What I would do next, in order**
1. Deploy and measure — the public URL is the biggest outstanding gap.
2. Live held-out evaluation once the model gate clears, reporting exact counts
   and misses rather than a rounded percentage.
3. Concurrency and cost measurement under realistic load.
4. Replace the offline planner's keyword rules with a small local model for CI,
   so the fallback path degrades more gracefully.
5. A richer clarification flow — today an ambiguous question is answered with a
   stated interpretation more often than it asks. Asking is sometimes better.

**The decision I am least sure about** is treating `metric_definitions.md`'s
"sources should never be mixed in a single ratio" as forbidding contamination
*within* a component rather than forbidding the documented formula itself. Read
literally the sentence prohibits its own formula. `data_source_guide.md` settles
it — "Market share always uses `distributor` in the numerator and `market_data`
in the denominator" — but it is an interpretation, and it is recorded as one in
`docs/ASSUMPTIONS.md#a3`.
