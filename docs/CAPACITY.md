# Capacity

How much load one deployment carries, where it saturates, and what a data
refresh costs while users are asking questions. The numbers come from
`scripts/load_test.py`, which anyone can run against a disposable copy of
the full dataset. The authoritative run is
`evidence/runs/r2-load-profile.json`.

**These are measurements on one development machine, not agreed service
levels.** No SLO has been set for this product. The targets below are
proposals to argue with.

**They are offline-pipeline capacity.** The planner here is deterministic
and makes no model call. These figures say nothing about live-model
throughput, latency, provider rate limits or cost. They measure everything
around the model: sessions, policy, compilation, the database, rendering
and persistence. See [Sensitivity](#sensitivity-a-live-model) for how a
live model changes them. That needs measuring, not deriving.

## The profile

| | |
|---|---|
| Data | A disposable copy of the full dataset (2,001,000 sales after the run's batches), stored in reporting-period order |
| Server | The image's shape: uvicorn, 2 workers, the application's own pools (8 exec, 8 scoped, 4 auth and 4 checkpoint connections per worker) |
| Database | PostgreSQL 16.14, same machine (Apple silicon, 10 cores) |
| Users | Throwaway: 6 RAMs in the busiest territories, 4 Directors, 2 Execs. Each virtual user signs in once and keeps its session |
| Role mix | 60% RAM, 30% Director, 10% Exec |
| Question mix | 45% cheap (one aggregate), 40% medium (rankings, growth, share by territory), 15% expensive (facility by month over all time; dense monthly series under row-level security) |
| Planner | Offline. **No model latency or cost is included** (see [Sensitivity](#sensitivity-a-live-model)) |
| Clients | Closed loop with no think time, so a stress test rather than real pacing. Per-user quotas were lifted, because otherwise each virtual user would be held to 20 requests a minute |

## Results

| Concurrent clients | Answers/s | p50 | p95 | p99 | Errors |
|---:|---:|---:|---:|---:|---:|
| 1 | 5.5 | 82 ms | 0.80 s | 0.84 s | 0% |
| 4 | 15.1 | 127 ms | 1.18 s | 1.28 s | 0% |
| 8 | **20.2** | 188 ms | 1.81 s | 1.89 s | 0% |
| 16 | 16.6 | 502 ms | 2.91 s | 3.53 s | 0.9% |
| 32 | 16.3 | 1.19 s | 4.02 s | 6.01 s | 5.2% |

p95 by question class at 8 clients:

| Class | p95 |
|---|---:|
| Cheap | 0.24 s |
| Medium | 0.85 s |
| Expensive | 1.90 s |

**Where it saturates.** Throughput peaks at about 8 concurrent clients.
From 16 upwards, all 16 scoped connections (two workers × 8) are active the
whole time, and requests queue for them. Server-side time accounts for
almost all of what the client sees, and database time for almost all of
the server's time. The pool is the backpressure point, by design. The
database does the work, and the pools keep it from being asked for more
than it can do at once.

**What fails, and how.** Every error at 16 and 32 clients was the 5-second
statement timeout on an **expensive** question. The user saw "That question
took too long to answer. Narrowing it — a shorter time period, a specific
product, or fewer groupings — will usually work." Nothing failed with an
internal error or a dropped connection. Cheap and medium questions stayed
answerable at 32 clients, but slowly: cheap ones at p95 2.5 s.

**This is a capacity boundary.** The 5% timeout rate at 32 clients shows
where this configuration stops answering everything it admits. Whether
that is acceptable depends on a service target that has not been set. What
it should inform:

- **Admission control.** Implemented since this profile was measured
  (see [Admission control](#admission-control) and its own measurement).
- **Concurrency per replica.** Size the number of users per replica from
  the 8-client knee, not from the 32-client figure.

**Variance.** Results at saturation move a lot between runs on this
machine. Across the runs made, throughput at 8 clients ranged from 12 to
24 answers a second. The other runs are recorded in
`r2-load-profile-before-settle.json`, `r2-load-profile-scrambled-table.json`
(both on a table an earlier `UPDATE`-based shift had disordered),
`r2-throughput-*.json` and `r2-load-failures.json`. Read the table above
as one run's shape, not as a constant.

**Compared with the earlier benchmark.** `docs/EVALUATION.md` reports 32
answers a second at 8 clients. That benchmark ran in process, without HTTP
or sessions, over ten lighter question shapes. This profile goes through
the full HTTP path and includes the two most expensive shapes, so the two
figures are not in conflict.

## Refreshing data under load

Measured at 8 concurrent clients:

| Publication | Took | Reader p95 during / outside it | Errors | `refresh` answers |
|---|---:|---:|---:|---:|
| 500 sales in the latest week | 39.6 s (incl. the 20 s settle) | 2.25 s / 2.20 s | 0.9% | 2 |
| 500 sales opening a **new** week (every offset rewritten) | 434 s (124 s with no load) | 2.99 s / 1.78 s | 0.5% | 0 |

Two things are shown here:

- **The generation check works under load.** Two requests planned before
  the in-week publication and executed after it were answered `refresh`
  rather than with mixed data.
- **No reader was blocked.** Errors during publication were the same
  statement timeouts on the expensive class.

A new-week publication is the expensive one, and it happens once a week.
Schedule the first batch of a new week outside working hours. Immediately
after it, throughput at 8 clients was 13.0 answers a second, against 20.2
before. After a pause, in a separate measurement, it was 17.6 against
19.3. Both are within this machine's run-to-run variance, so a lasting
cost is not established. [INGESTION.md](INGESTION.md#measured-limits)
describes two refresh problems this profile found and how they were fixed:
replaced rows not being reclaimed, and the shift scrambling the table's
order.

## Admission control

`app/admission.py`. Work is admitted in two places per worker process, and
refused early and predictably rather than late, by a timeout:

| Stage | Limit (per worker) | Beyond it |
|---|---|---|
| Questions in flight | `PAC_ADMISSION_MAX_INFLIGHT_REQUESTS` = 24 | 503 `overloaded` at once, before the body is read |
| Analytical queries running | `PAC_ADMISSION_QUERY_SLOTS` = 4 | wait in the queue |
| Queries waiting | `PAC_ADMISSION_QUERY_QUEUE` = 16, first come first served | 503 `overloaded` at once |
| Longest wait | `PAC_ADMISSION_QUERY_WAIT_SECONDS` = 10, never past the request deadline | 503 `overloaded` (or "took too long" if the deadline ran out) |

The query limits are sized from the profile above. Throughput peaks around
8 concurrent queries per replica, which is two workers with 4 slots each.
Across a deployment the totals multiply by workers and replicas.

- **Fairness.** One user holds at most 2 places, because the per-user
  concurrency limit is counted in the database across workers. The queue
  is first-come-first-served, so later arrivals cannot starve anyone. The
  per-user rate counts every attempt that does work, including a retry of
  a request that failed, was abandoned or was cancelled. Replaying a
  committed answer is free. Before the fix for review R2 (1 October 2026), a
  retry skipped these limits (`tests/security/test_retry_quotas.py`).
- **Cancellation.** A cancel ends a wait at the next check, within about
  0.25 s.
- **A refused question retried** with the same idempotency key resumes
  after planning, so the model is not paid for twice.
- **The interface** waits out `Retry-After` with jitter before retrying.

Tested: `tests/unit/test_admission.py`,
`tests/integration/test_admission_pipeline.py` and the 503 cases in
`tests/security/test_failure_responses.py`.

**Measured** with the same profile, on the same machine, with admission on
at its defaults (`r2-load-admission.json`). Clients honour `Retry-After`
with jitter, as the interface does.

| Clients | Without admission (`r2-load-profile.json`) | With admission |
|---:|---|---|
| 8 | 20.2 answers/s, p95 1.8 s, 0 errors | 24.8 answers/s, p95 1.2 s, 0 errors |
| 16 | 16.6/s, p95 2.9 s, **0.9% statement timeouts** | 23.0/s, p95 1.6 s, 0 errors |
| 32 | 16.3/s, p95 4.0 s, **5.2% statement timeouts** | 20.5/s, p95 2.5 s (p99 5.0 s), 0 errors |
| 64 | not measured | 20.2/s answered, p95 3.2 s; **29% refused** with 503 `overloaded`, p95 82 ms; 1 statement timeout (0.1%) |

What changed:

- **Overload no longer arrives as a timeout.** Below the limits,
  questions wait their turn and are answered. At 32 clients the queue
  absorbed everyone. At 64, the excess was refused at once (82 ms) with a
  retry time. Before admission, it waited and then timed out after
  5 seconds.
- **Throughput holds instead of falling.** Without admission, 16 and 32
  clients answered fewer questions a second than 8 did, because contention
  slowed everyone. With admission it stays near the 8-client peak.
- **The single timeout at 64** is an expensive question that took over 5 s
  even with only 4 queries running per worker. Admission bounds contention;
  it does not make one question faster.

These are separate runs on a machine whose throughput varies by up to 2x
between runs, so read the shape, not the exact figures.

## Freshness

How old an answer's data can be is the sum of three delays, and only the
last is measured here:

1. **Source arrival.** The time from a sale happening to its batch reaching
   ingestion. This is set by the feed, and no real feed exists yet.
   `pac.ingest.lag` records it per source once one does
   ([OBSERVABILITY.md](OBSERVABILITY.md)).
2. **Batch schedule.** How often batches are run.
3. **Publication.** Measured above: about 40 s for an in-week batch under
   load, about 7 minutes for the first batch of a new week under load
   (124 s idle).

So any promise of freshness has the form "a sale is visible within
(source delay) + (schedule interval) + 7 minutes". It cannot be shorter than
the new-week publication time unless that rewrite is scheduled outside
working hours or removed (see [INGESTION.md](INGESTION.md#measured-limits)).
`GET /api/me` reports what the data runs through and when it was published,
so users can see the age of what they are reading.

## Provisional targets

These are proposals, not commitments.

| | Proposal | Measured here (8 clients) |
|---|---|---|
| p95, cheap and medium questions | < 2 s | 0.24 s / 0.85 s |
| p95, expensive questions | < 5 s, the statement timeout | 1.90 s |
| Error rate | < 1%, excluding refusals and clarifications | 0% |
| Data refresh | Never blocks readers; a new-week batch outside working hours | as measured above |

Rough sizing: a sales organisation where every user asks one question a
minute at peak generates `users ÷ 60` questions a second. At the 20 a
second measured here, that is about 1,200 users at that rate per replica,
before model latency (below). One replica does not provide availability,
only capacity. See [RUNBOOK.md §9](RUNBOOK.md) for connections and replicas.

## Sensitivity: a live model

The live planner adds one model call per question. The earlier live runs
measured p50 3.5 s and p95 4.7 s, using about 4,670 input and 160 output
tokens. A repaired plan costs a second call.

- **Latency:** add the planner's time to every figure above. Planning
  happens before any database connection is taken, so model latency does
  not occupy the pools.
- **Concurrency:** a request waiting on the model holds a worker thread,
  not a database connection. At 3.3 questions a second (200 users at one a
  minute) with 5 s of planning, about 17 requests are waiting on the model
  at once (Little's law). That is well inside the two workers' thread pools
  and the per-user limit of 2 concurrent requests.
- **Cost per answer:** `4,670 × input rate + 160 × output rate`, per
  million tokens, at the contracted rates (`PAC_LLM_INPUT_USD_PER_MTOK`,
  `PAC_LLM_OUTPUT_USD_PER_MTOK`). With those set, `pac.llm.cost` reports it
  ([OBSERVABILITY.md](OBSERVABILITY.md)). No rate is assumed here.
- **Provider limits:** a provider rate limit produces "busy or unreachable"
  after the SDK's own retries ([API.md](API.md)). The provider's quota, not
  this system, then bounds throughput.

## Not measured

- The deployed host, a managed database, or more than one replica.
- Live-model latency under concurrent load, and provider rate limits at
  volume.
- Real user pacing, think time and question mix.
- Long soak: hours of load across several weekly publications.
