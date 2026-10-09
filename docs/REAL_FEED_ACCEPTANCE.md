# Accepting a real feed

What must be true before this system answers questions about a customer's own data,
or keeps it current from a feed. **No real feed has been connected.** Everything
marked *locally verified* below was exercised on the supplied dataset and on two
synthetic fixture profiles (`scripts/fixture_profile.py`), which establishes how
the system handles that shape of data, not that a given feed has it.

Status words: **implemented**, **locally verified** (a test or record on this
machine), **blocked** (needs the feed or the data owner), **owner decision**.

## How a feed is assessed

1. Load a first extract into a disposable database: `scripts/load_data.py --mode full
   --generated-dir <extract> [--classification-mapping <file>]`. The load refuses what
   would be wrong to publish and warns on what answers must disclose.
2. Write the readiness report: `scripts/readiness_report.py --db <database> --real-data yes
   --out readiness.json`. Each section says `ready`, `attention`, `blocked` or
   `not_measured`, with the evidence it measured (`app/data/readiness.py`).
3. Send a first batch through ingestion and reconcile every event against what the
   feed sent (`scripts/build_profile_db.py` shows the reconciliation for a profile).
4. Walk the checklist below with the data owner. Every `attention` item is either
   accepted (its caveat stays in the answers) or fixed in the extract.

## Checklist

| # | Item | What the system does | Verified | Needs |
|---|---|---|---|---|
| 1 | **Identifiers.** `ndc` and `org_id` are stable across extracts and batches; names are labels | Groups by id, never by name; two facilities with one name stay apart; a sale naming an unknown org or product is refused (load) or quarantined (batch) | locally verified (profiles: shared names, one drug name over two strengths) | blocked: the owner confirms ids are stable over time |
| 2 | **Week and month convention.** Week endings on one weekday; week labels the ISO week, ISO year included; months by one rule | The load records whether ingestion can extend the calendar (`source_coverage.calendar`) and warns `calendar_not_extendable` if not; ingestion places a sale by the convention or quarantines it as ambiguous | locally verified (week-53 relabelling: `test_calendar_convention_at_load.py`) | blocked: the feed's own labels |
| 3 | **Time zone.** Event timestamps carry an offset; the business day is taken in `PAC_BUSINESS_TIMEZONE` | A timestamp without an offset is quarantined (`naive_timestamp`) | locally verified (contract tests) | owner decision: which time zone defines the business day |
| 4 | **Source totals and overlap.** What each `data_source` covers, and whether market data includes the company's own volume | Measures rows and packs by source, months lacking a source (unknown, never zero), company rows in market data; answers carry the caveats | locally verified (profiles: market data missing for whole months) | blocked: the owner states what each source covers |
| 5 | **Units.** `pack_units` are packs; `unit_conversion_factor` converts to equivalents | A product without a positive factor is excluded from equivalents and the answer says so | locally verified | blocked: the owner confirms each factor |
| 6 | **Money.** `wac` is the transaction's dollar amount (A8), positive for distributor rows, zero for free drug | The readiness report shows the WAC-per-pack band per NDC: narrow bands are consistent with A8 but cannot prove it | implemented; consistent on supplied and profile data | **owner decision**: confirm A8 for the feed |
| 7 | **Classification.** Which products are generics, biosimilars or branded competitors | Only `brand_flag = 1` is taken from the source; every other class comes from a versioned mapping or is `unknown`; a contradicting mapping is refused; segment shares carry unknown volume as a range | locally verified (`r5-classification-*`; profiles with and without a mapping) | blocked: the owner supplies a classification mapping |
| 8 | **Hierarchy.** Facility, parent, top organization; ZIP to territory | Accounts roll up to the top organization present; unmapped facilities are visible only to Exec totals, labelled unmapped | locally verified (profiles: standalone facilities, parents without a top organization, an unmapped ZIP) | blocked: the owner's hierarchy and territory alignment |
| 9 | **Territory access.** Each user's territory or region name matches the geography; names are unique | A reused territory or region name is refused at load; the report lists scoped users who would see nothing | locally verified (`r5-territory-name-*`) | blocked: the identity provider's role and territory claims for real users |
| 10 | **Cadence and lateness.** How often batches arrive, how late events can be | Watermarks, lag and late-event counts per batch; freshness metrics and alerts | locally verified on synthetic batches only | blocked: a real feed's schedule; thresholds below |
| 11 | **Correction and deletion identity.** Every event has `(source_system, source_event_id)` and a version; deletions are tombstones | Replays are ignored, a higher version corrects, a tombstone deletes (even before its sale arrives), a conflicting version is quarantined, every event reconciles | locally verified (profiles: 98 events reconciled each) | blocked: the feed must provide stable event identity |
| 12 | **Contract thresholds.** Below | Applied to every batch | implemented | owner decision: set from the feed's distribution |

## Contract thresholds in force (provisional)

None of these was set from a measured feed. Each is an engineering bound chosen so
that no plausible sale reaches it; each is reported as `provisional` in every
readiness report, and should be revisited once a real feed's distribution is known.

| Threshold | Value | Where |
|---|---|---|
| Packs per event | at most 1,000,000 | `app/data/sources.py` `MAX_PACKS_PER_EVENT` |
| WAC per pack | at most 10,000,000 | `MAX_WAC_PER_PACK` |
| Events per batch | at most 100,000 | `MAX_EVENTS_PER_BATCH` |
| Identity length | at most 256 characters | `MAX_IDENTITY_LENGTH` |
| Quarantined share before a batch is rejected whole | 5% | `PAC_INGEST_MAX_QUARANTINE_RATIO` |
| Business time zone | America/New_York | `PAC_BUSINESS_TIMEZONE` |

## Not claimed

- That any real feed has been loaded, ingested or reconciled.
- That the supplied classification mapping is correct beyond the supplied document
  it was curated from.
- That synthetic cadence, lateness or volume resemble a real feed's.
