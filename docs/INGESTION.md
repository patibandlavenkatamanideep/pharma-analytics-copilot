# Incremental ingestion

How new, corrected and deleted sales reach a published dataset without a
full reload, and what the system promises about them. The code is
[app/data/ingest.py](../app/data/ingest.py) and
[app/data/sources.py](../app/data/sources.py). The tests are
[tests/integration/test_ingestion.py](../tests/integration/test_ingestion.py),
which applies real batches to a disposable database, and
[tests/unit/test_ingest_calendar.py](../tests/unit/test_ingest_calendar.py).

A full or seed load (`scripts/load_data.py`) still defines the base dataset.
Incremental batches extend that base. They do not replace it.

## Shape: micro-batches, not a stream

A source delivers **batches** of **events**. Each batch is reconciled,
validated, applied and published in one owner transaction. Nothing here
needs anything faster: answers are weekly and monthly aggregates, and the
supplied facts carry week and month offsets that must be rewritten whenever
the latest week moves (see [Measured limits](#measured-limits)). There is no
queue, broker or second datastore. The ledger and the batch log are
PostgreSQL tables in the `app_ingest` schema
([migration 018](../migrations/018_ingestion.sql)).

## The event contract

| Field | Meaning |
|---|---|
| `source_system` | Which feed the batch came from. Identity is per feed |
| `source_event_id` | The feed's own id for the sale, stable across corrections |
| `event_version` | A positive integer. A higher version supersedes a lower one |
| `kind` | `upsert` or `delete` |
| `event_time` | When the sale happened, **with a UTC offset**. A naive timestamp is quarantined, never assumed to be UTC or local time |
| `org_id`, `ndc` | Must already exist. A batch cannot create organizations or products |
| `data_source` | `distributor`, `hub_dispense` or `market_data` |
| `pack_units` | Positive |
| `unit` | Must be `packs`. Anything else is quarantined, never converted |
| `wac` | Non-negative. Positive for `distributor`. Zero for `hub_dispense`, which is free drug |

A batch also declares **control totals**: how many events it contains and
the sum of their packs. Every event counts towards the totals, including
invalid ones.

Each event becomes an ordinary `sales` row. The derived columns are filled
the way the supplied data fills them, which was checked against the full
dataset:

- `drug_name`, `brand_flag` and `specialty` come from `products`.
- `state` comes from `organizations`.
- `total_mg` is `pack_units × mg_equivalent`.

No business table gains a column, and no existing column changes meaning.

### Business day and timezone

`event_time` is converted to `PAC_BUSINESS_TIMEZONE` (default
`America/New_York`) to get the transaction date. That date decides the week
and the month. A sale at 22:30 on a Sunday in New York belongs to Sunday,
even though it is already Monday in UTC. This is tested.

## Identity, versions, corrections and deletions

The ledger records one row per `(source_system, source_event_id)`: the
version applied, a digest of its content, and the `sales` row it produced.
That key is the primary key, and each `sale_id` can appear in the ledger at
most once. The database enforces both.

Each incoming event, compared with the ledger, is exactly one of the
following:

| Incoming | Ledger has | Outcome |
|---|---|---|
| any version | nothing | **insert** (or, for a delete, a tombstone with nothing to remove) |
| higher version, upsert | a live sale | **correction**: the same row is updated |
| higher version, delete | a live sale | **tombstone**: the row is deleted and the ledger remembers the deletion |
| higher version, upsert | a tombstone | the sale is **reissued** as a new row |
| same version, same content | — | **duplicate**: nothing happens |
| same version, different content | — | **quarantined** as `conflicting_versions` |
| lower version | — | **duplicate**: an older copy never undoes a newer one |

What follows from that:

- **Replaying a batch changes nothing.** Every event is a duplicate. The
  batch is recorded as `no_change` and no new generation is published.
- **A correction applied twice updates the fact once.**
- **A late copy cannot bring a deleted sale back**, because its version is
  not higher than the tombstone's. A deletion that arrives before its sale
  also holds: the sale arrives with a lower version and is a duplicate.
- **Ordering is by version, not by arrival.** Out-of-order delivery gives
  the same final state.
- **Amounts never decide identity.** Two sales of 12 packs at the same
  account at the same moment, with different ids, are two sales. Nothing
  deduplicates with `DISTINCT` or by comparing amounts.

Event identity has nothing to do with request idempotency keys
(`app_conv.runs`) or entity-name resolution. They are separate mechanisms
and share no code.

After applying a batch, ingestion checks that the change in total packs
equals what the events say it should be. Inserts add their packs,
corrections add the difference, and deletions subtract the old amount. If
the two disagree, the batch is rolled back.

## Validation, quarantine and reconciliation

The steps run in this order, and anything that refuses the batch refuses it
**before the first write**:

1. **Reconciliation.** If the event count or the pack total differs from the
   declared control totals, the whole batch is `rejected`. An incomplete
   delivery is never published as a complete one.
2. **Validation.** Each event is checked against the contract above.
   Failures are written to `app_ingest.quarantine` with a reason, the
   attempt number and the payload. The reasons are `invalid_identity`,
   `invalid_kind`, `missing_field`, `naive_timestamp`, `unit_not_packs`,
   `unknown_data_source`, `unknown_organization`, `unknown_product`,
   `non_positive_packs`, `invalid_price`, `priced_free_drug`,
   `future_event`, `before_history`, `period_convention_ambiguous`,
   `calendar_conflict` and `conflicting_versions`.
3. **Quality threshold.** If more than `PAC_INGEST_MAX_QUARANTINE_RATIO`
   (default 5%) of a batch is quarantined, the batch is `rejected` whole: the
   feed is treated as broken. Below the threshold, the valid events are
   applied and the quarantined ones wait. They can be sent again later
   under the same ids.
4. After applying, the loader's full validation runs again over the
   resulting data: foreign keys, sources, conversion factors, coverage,
   anchor. It runs in the same transaction, so a fatal finding rolls the
   batch back.

Each attempt's outcome is kept in `app_ingest.batches`: declared and
received totals, quarantined, applied, corrected, tombstoned and duplicate
counts, late events, affected periods, anchor movement, and the dataset the
batch published. `scripts/ingest.py` prints the same as one JSON line per
batch. The serving role can read this table and the watermarks, but cannot
write to either.

If a failure happens after writing has begun, the whole transaction rolls
back. The attempt is then recorded as `rejected`, with the error, in a
separate transaction.

## The calendar: placing a sale in a week and a month

The supplied facts store period labels (`period_wk`, `period_mo`,
`period_qtr`) and offsets (`wk_offset`, `mo_offset`) on every row.
Ingestion does not invent conventions for these. It uses the ones the
published calendar already follows:

- **Week ending.** Every published week ends on the same weekday. The full
  dataset ends weeks on Saturday and the seed on Sunday. A sale belongs to
  the week ending on or after its business day.
- **Week label.** The ISO week of the week-ending date. Both datasets
  follow this.
- **Month.** One of two rules: the month of the week-ending date (the full
  dataset), or the month that holds most of the week's days (the seed). A
  rule is used only if *every* published week agrees with it. If both rules
  fit the history but disagree about a new week, that event is quarantined
  as `period_convention_ambiguous` rather than guessed.
- **Quarter.** The quarter of the period month.

If the published calendar fits no rule, the batch is refused with the
reason. Examples are mixed weekdays, a label that is not the ISO week, a
month carrying two offsets, or offsets that do not fall as weeks get later.

A sale in a week the calendar **already has** takes that week's published
labels and offsets. An increment never relabels history.

A sale in a **new** week is labelled by the rules above. Its offset is the
distance in weeks from the latest week. It must fit between its neighbours
and must not take an offset another week already has. Otherwise it is
quarantined as `calendar_conflict`. The supplied seed shows why this check
exists: its offsets are not distances. 30 Aug is offset 4 although it is
three weeks back, so a sale in the week ending 23 Aug has no consistent
place, and it is refused.

A sale before the first published week is `before_history`. The base load
defines how far back history goes.

### When the latest week or month moves

Offsets count back from the latest period present: offset 0 is the latest
week and the latest month. When a batch adds a later week, every `sales`
row's `wk_offset` increases by the number of weeks the anchor moved. When it
adds a later month, every `mo_offset` increases too. If a tombstone empties
the latest week, the anchor moves back. The calendar is then rebuilt from
the facts and the manifest's reporting anchor is recomputed, all in the same
transaction. "Last month" therefore means the same thing in the calendar,
the facts and the manifest at every moment.

A week inside the range that has no row in any source is reported as a
`calendar_gaps` warning on the manifest. It is never shown as a zero. This
is the loader's existing coverage rule, and ingestion inherits it.

### Late events and affected periods

An event in a week earlier than the latest is counted as **late**. The
months it touches are recorded in `affected_periods`; a correction that
moves a sale records both the old and the new month. Nothing else needs
recomputing, because every aggregate is computed at query time from the
facts. There are no materialised totals to refresh. The rebuilt calendar
recomputes each week's source coverage.

## Publication

A batch that changes facts publishes a **new generation** in the same
transaction as the facts:

- a new `app_meta.dataset_manifest` row. `load_mode` stays the base's
  (`seed` or `full`), and the row records `parent_dataset_id` and
  `ingest_batch_id`;
- the previous generation marked `superseded`;
- `app_ref.generation` flipped to the new id.

A request already running read its generation and facts under one
repeatable-read snapshot ([Phase 5A](PRODUCTION_UPGRADE.md)). It either
finishes on the old generation, complete, or is told `refresh` if it planned
on the old one and executes after the flip. Vocabulary and entity caches are
keyed by dataset id, so every worker drops them on the next request. After
commit, `VACUUM (ANALYZE)` reclaims the dead row versions left by
corrections and offset rewrites.

Full loads, seed loads and incremental batches take the same advisory lock
(`pac:publication`). Two publications never interleave. This is tested: a
batch waits while another publication holds the lock.

A full or seed load clears the ledger and watermarks, because the new base
does not contain the old increments. The same events sent again therefore
apply again. Batch history is kept.

## Freshness

`app_ingest.watermarks` holds, per source, the latest event time applied
and when the feed last delivered a batch, whether or not the batch changed
anything. `GET /api/me` reports:

- `data_through`: the latest transaction date in the data;
- `published_at`: when this generation was published;
- `incremental`: whether it was built by a batch;
- `last_ingest_at`: when any feed last delivered.

The interface shows "data through …" in the header and the times on hover.

## Attribution versus access

A sales row records `org_id` and nothing about territory, region or parent
system. Those are resolved **at query time** from the current
`organizations` and `zip_territory` tables, so history follows the
**current** hierarchy. If an account moves to another territory, its history
moves with it. The user who now covers the account sees that history, and
the previous territory's totals change. This is the supplied model's
semantics, and ingestion does not change it.

Access is always **current**. Row-level security filters every query,
historical or not, by the requesting user's current scope. A sale ingested
for an account outside a RAM's territory is invisible to that RAM (tested).
A report "as the hierarchy was in March" is not supported. It would need the
hierarchy versioned with effective dates, which the supplied schema does not
have.

## Sources

`SourceAdapter` is the interface: anything with a `source_system` and a
`batches()` iterator.

- `JsonBatchFiles` reads one JSON document per batch. Its format is in the
  class docstring.
- `SyntheticIncrementalSource` produces deterministic batches from the
  organizations and products that exist. For a given seed, the same batches
  are produced again, which makes replay testable. It exercises the real
  path, not a mock of it.

Payloads are not stored server-side. Only their digests are kept, plus the
full payloads of quarantined events. **A production source must keep its
batches** so that they can be replayed after a restore or a base reload.

## Operation

```bash
# In the jobs container (owner credential), like migrations and loads:
python3 scripts/ingest.py batch-0001.json batch-0002.json
python3 scripts/ingest.py --synthetic --seed 7        # demonstration feed
```

The exit status is 1 if any batch was rejected. To inspect outcomes:

```sql
SELECT batch_id, status, attempts, applied, corrected, tombstoned, duplicates,
       quarantined, late, affected_periods, rejection_reason
FROM app_ingest.batches ORDER BY last_attempt_at DESC LIMIT 20;

SELECT reason, count(*) FROM app_ingest.quarantine
WHERE batch_id = 'batch-0002' GROUP BY 1;
```

**Recovery.** A rejected batch changed nothing. Fix it at the source and
send it again with the same `batch_id`; it is processed again from the
start. A published batch that turns out to be wrong is undone **forward**,
by sending corrections or deletions at higher versions. The previous
generation's facts are not retained, so there is no instant rollback to a
parent generation. The heavier alternative is to reload the base and replay
the retained batches up to the last good one.

## Measured limits

Measured on 2026-09-30 on the development machine (Apple silicon, 10 cores,
PostgreSQL 16.14). The data was a disposable copy of the full 2,000,000-row
dataset; the working database was not touched. The reproducible command is
`scripts/measure_ingestion.py`, which refuses to run against the working
database. The record is `evidence/runs/r2-ingestion-scale.json`.

| Batch | Time |
|---|---:|
| 500 new sales in the latest week (anchor unchanged) | 6.7 s |
| The same batch replayed (`no_change`) | 0.14 s |
| 500 new sales in a **new** week (every `wk_offset` rewritten) | 133 s (108 s in an earlier run) |

The new-week case is dominated by one `UPDATE` of all 2,000,001 rows, which
took 103 s when timed on its own, followed by a `VACUUM` of about 17 s. The
offset columns are indexed and stored on every fact, so the update cannot be
a heap-only update. It happens once per reporting week, when the first sale
of a new week arrives.

Readers are not blocked during it. While the rewrite was in flight, a
representative month query sampled every second (121 samples) had a median
of 53 ms against 18 ms idle, and a worst case of 971 ms. That is I/O
contention, not a lock wait: the statement timeout is 5 s and no sample
failed. Other publications wait on the advisory lock for the whole time.

The cost is a consequence of the supplied contract, which stores offsets on
each fact. Calendar-relative offsets resolved at query time would remove it,
but that changes how the supplied columns are used, so it is recorded here
rather than done.
