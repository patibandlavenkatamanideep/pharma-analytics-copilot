# Provisional targets — decision record

**Status:** assumptions, chosen 7 October 2026 so that this candidate can be judged
against something. They are **not service-level objectives**: no owner has agreed them,
and the deferred checklist asks for that agreement. Each is labelled with what it is
judged against and whether the latest local measurement meets it.

**Context.** The assignment sets no traffic, freshness or recovery targets, and the
deployment owner has not been asked (decision 4 of the original brief). CAPACITY.md
proposed numbers to argue with; this record picks them as working assumptions, adds
freshness and recovery, and says what each rests on.

## Service (one replica of the image: 2 workers; offline planner)

| Target (assumption) | Why this number | Judged by | Measured on this candidate (`r5-load-profile.json`) | |
|---|---|---|---|---|
| p95 of cheap and medium questions **< 2 s** at up to 8 concurrent questions per replica | the knee of the profile; a person waits about that long for a table | `load_test.py`, client side, under RLS | 8 clients: cheap 0.35 s, medium 0.78 s | met |
| p95 of expensive questions **< 5 s** | the statement timeout: an expensive question either answers within it or says so | same | 8 clients: 4.45 s; 16 clients: 2.29 s | met, close |
| Errors **< 1%** of questions, excluding refusals, clarifications and denials, up to 32 concurrent | an error is a question the user must ask again | same; `Errors` alert at 5% | 0% at 1–16; 0.14% at 32 (one timeout) | met |
| Beyond capacity, the excess is **refused at once** (503 with `Retry-After`, p95 < 1 s), not left to time out | overload should be visible and retryable | same; `PoolExhausted`, admission metrics | 64 clients: 31.5% refused, refusal p95 0.10 s; 0.23% timeouts | met |
| Answer time **including the model**: p95 < 10 s | the `SlowAnswers` alert threshold | a live evaluation | **unmeasured** for prompt 2.3.0 — no model call is authorized; the 4.7 s planning p95 in EVALUATION.md belongs to an earlier prompt | blocked |

Sizing (assumption, offline): about 20 answers a second per replica at the knee, so
roughly 1,200 users asking one question a minute; replicas multiply query admission, not
the per-user limits (RUNBOOK.md §9).

## Freshness

| Target (assumption) | Why | Judged by | Measured | |
|---|---|---|---|---|
| A batch inside the current week is **published within 2 minutes** of its run, under load, without blocking readers | an in-week correction should reach users the same hour | `load_test.py` publication phases | 32.6 s; reader p95 1.93 s during, 1.94 s outside; 0 errors | met |
| The first batch of a new week is **published within 15 minutes** under load and **scheduled outside working hours** | it rewrites every row's offsets once a week | same | 320 s (434 s on 1 October); reader p95 2.12 s during, 2.01 s outside; 0.57% expensive timeouts during it | met |
| Feed cadence **daily**; `MissedIngestionRun` after 26 h | no real feed exists; a daily distributor file is the common case | the alert (drill: fires and clears) | — | assumption |
| Data age ≤ source delay + 24 h + publication | the sum of the three delays (CAPACITY.md) | `pac.ingest.watermark_age`, `GET /api/me` | unmeasured: no real feed | blocked |

The 434-second new-week publication was revisited because ingestion code changed since
it was measured: 320 s now, inside the budget, so its duration is not optimized further.
Its lasting cost was measured instead. The shift deletes and re-inserts every row, so the
facts table and its indexes are left at **twice their size** (heap 356 → 711 MB, indexes
776 → 1,550 MB; order kept). Over a 10-minute soak right after it (`r5-load-soak.json`)
nothing drifted (p95 about 2.3 s every minute, worker memory flat near 145 MB), but
expensive questions timed out at **0.99%**, the edge of the error target. `VACUUM (FULL,
ANALYZE) sales` (`evidence/probes/compact_facts.sh`) took **33 s**, during which every
reader waits, and restored the size; at 8 clients errors then fell to 0.11% and p95 to
1.58 s. Throughput did not change with it: 14.6 answers a second compacted against 15.5
on a never-published copy measured two minutes later (`r5-load-control-fresh.json`). The
earlier 22 against 13 mixed this machine's variance into the comparison. **Assumption:**
compact `sales` in the same out-of-hours window as the new-week batch.

## Recovery

| Target (assumption) | Why | Judged by | Measured | |
|---|---|---|---|---|
| **RPO 24 h**: a nightly `pg_dump`, with source batches kept by the feed for replay | the simplest backup that loses at most a day; tighter needs PITR or a managed database | the backup schedule (not configured here) | procedure verified; no schedule exists | assumption |
| **RTO 1 h** from a backup to a serving database on a new server, excluding provisioning the server | restoring is minutes; finding the backup and repointing the service dominate | `restore_drill.py --new-cluster` locally; a staging drill for the rest | restore 11.6 s, restore to ready with every check 17.0 s for 2,000,000 sales (new cluster, same host) | met locally; the rest unmeasured |
| Recovery path: **forward fix**, restore for lost data, no code rollback | [ROLLBACK_DECISION.md](ROLLBACK_DECISION.md) | the decision record | upgrade with live state and restore into a new cluster both verified | met locally |
| **Availability: none promised.** One replica and one database are a single point of failure; any target needs two replicas and a database with failover | a number without that topology would be fiction | — | — | owner decision |

## What would change these

The owner's actual user count and pacing, the feed's real cadence and delay, the model
and its provider limits, whether data may leave the environment, and the business's
tolerance for a day's data loss.
