# Audit decision record

**Status:** decided 7 October 2026 for the code; the *policy choice* for a deployment
remains the product or compliance owner's. Default unchanged (best effort); a strict
mode is available and opt-in: `PAC_AUDIT_MODE=strict`.

**Context.** `app_meta.query_audit` is the record of who asked what and what the
system decided (hashes, codes, counts, versions, timings, never values or text). The
assignment asks for access control, not an audit regime, and no owner has stated
whether every released answer must have a durable audit record. Until now the write was
best effort, documented and pinned by a test, with two replacements described and
neither built ([OBSERVABILITY.md](OBSERVABILITY.md#audit-durability-the-current-policy-and-the-decision-it-needs)).

## Options

| | Best effort (default) | Atomic | Fail closed |
|---|---|---|---|
| When the row is written | After the turn commits, in its own transaction | In the transaction that commits the turn and the run's outcome | Before the answer is released |
| Audit storage refuses or is down | Answer released; failure logged and counted (`pac.persistence.failures{kind="audit"}`); no row | Turn and row both fail to commit; on its own this still releases the answer, marked not saved | Answer withheld |
| Worker dies after the commit, before the audit write | Committed, replayable answer with no row | Cannot happen: they are one commit | Depends on how it is built |
| Replay of a committed outcome | Not recorded separately | Not recorded separately | Must be recorded, or withheld |
| Cost | A released answer can lack its record | Couples audit to conversation persistence | An audit-storage problem becomes an answer outage |

Atomic alone does not stop an unrecorded release, because a failed commit still
returns the answer marked not saved. Fail closed alone, as a separate write, can leave a
committed outcome that a replay releases without a record. **Strict mode is both:**
the row commits with the turn and the outcome, nothing is released unless that commit
succeeded, and a replay is recorded before it is returned.

## Decision

- **`best_effort` stays the default.** Availability of answers is preferred until an
  owner requires otherwise. Its gaps are stated, pinned by tests and alerted on.
- **`strict` is implemented and opt-in.** A deployment whose owner requires durable
  audit evidence for every released answer sets `PAC_AUDIT_MODE=strict`.

## What strict mode guarantees

No answer, fresh or replayed, leaves the server unless an audit row for that release
is committed:

- A fresh answer's row is written by `state.finalise` inside the transaction that
  commits the turn, its cohort and clarification, the conversation revision and the
  run's outcome. If that transaction does not commit, nothing does: the answer is
  withheld (`503 audit_unavailable`, `Retry-After`), the run is closed as failed, and a
  retry under the same idempotency key runs it again from its last checkpoint.
- A replay writes its own row (`status = replayed`, `replay_of` = the request that
  computed it, the same `run_id`) before it is returned; if that write fails, the
  replay is withheld.
- Every row carries `run_id` and `audit_mode` (migration 024), so the attempts and
  releases of one request can be followed.

What it does **not** guarantee:

- That the client received the answer. A recorded release can be lost on the wire; the
  retry then replays it, and that is recorded too.
- That an answer recorded was released. If the connection is lost after `COMMIT` is
  sent, the answer is withheld although its row and outcome exist; the retry releases
  it as a recorded replay. Recorded but not released is the conservative direction.
- A row for responses that release no data. Refusals before a run starts (busy, key
  reused, access changed, rate limited, overloaded) are counted in telemetry, not in
  the audit table; outcomes that commit no turn (a cancellation, a revision conflict,
  a failure before any outcome) are written best effort. Clarifications, denials and
  errors that commit a turn are written in the commit, like answers.
- Anything about telemetry, which never stands in for the audit trail.

## Evidence

`tests/security/test_audit_modes.py` (24 tests; `r5-audit-modes.json`), in both modes
where they differ:

| Scenario | Best effort | Strict |
|---|---|---|
| Database refuses the insert | answer released, failure counted, no row (`test_audit_contract.py`) | 503; nothing committed; retry commits once, one row |
| Storage outage at commit | answer released, marked not saved, no row, counted | 503; nothing committed |
| Worker killed before the commit | nothing committed; retry answers, one row | same |
| Worker killed inside the open transaction | rolled back; retry answers, one row | same |
| Worker killed after the commit | **committed, replayable, no row; the replay has none either** | row committed with the outcome; replay recorded |
| Lost response, retried | replayed, one row | replayed, second row `replayed` |
| Replay whose row cannot be written | — | replay withheld; released once the row can be written |
| Duplicate request while the first runs | refused (409), nothing released | same |
| Expired lease, another request commits | stale answer withheld (`conflict`) | same |
| Access revoked before a replay | replay refused (403), no release | same, no `replayed` row |
| Cancel before the answering step | nothing released; row `cancelled` | same |
| Cancel arriving during the commit | too late: answered, released, row `answered` | same |
| Commit succeeds, acknowledgement lost | answer released marked not saved; one row | withheld although recorded; the retry releases it as a recorded replay; the key commits once |

Five mutants of the strict path, each caught (`evidence/probes/audit_mutants.sh`,
`r5-audit-mutants.json`). Worker death is a child process exiting without cleanup
against one local PostgreSQL, not a killed container or a lost host.
