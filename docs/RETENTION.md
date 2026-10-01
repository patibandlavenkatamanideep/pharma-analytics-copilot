# Memory and retention

What the system keeps about a user and their conversations, for how long,
and how a user exports or deletes it. The code is
[app/conversation/privacy.py](../app/conversation/privacy.py) (export and
deletion, owner-scoped) and [app/retention.py](../app/retention.py) (the
scheduled periods). The tests are
[tests/security/test_data_rights.py](../tests/security/test_data_rights.py).

## What is remembered, and what is not

A conversation remembers **typed structure**, not transcripts:

- **Previous plan.** The metric, dimensions, filters, window and ranking.
  Its free-text fields, which the model writes (`interpretation`,
  `clarification`), are dropped before any planner sees it. See
  [AI_SYSTEM_DESIGN.md §8](AI_SYSTEM_DESIGN.md#8-prompt-injection).
- **Cohort.** The ids a "those accounts" refers to, stored whole with their
  dimension.
- **Pending clarification.** The choices shown, and the paused workflow
  thread a reply resumes.

None of this is authority. Role, scope and pricing access are read from
`users` on every request. A conversation recorded under access the user no
longer holds is neither continued nor listed, and it is not exported.

## Where it lives

| Store | Holds | Deleted by a user | Retention |
|---|---|---|---|
| `app_conv.conversations`, `turns` | Questions, the answers shown, statuses | yes | 180 days since last activity |
| `app_conv.cohorts`, `cohort_members` | Populations for follow-ups | yes, with the conversation | with the conversation |
| `app_conv.clarifications` | Questions asked back and their choices | yes, with the conversation | expire after 30 min; deleted with the conversation |
| `app_conv.feedback` | Ratings, a reason, and an optional comment, on your own answers | yes, with the conversation | with the conversation |
| `app_conv.runs` | Request records, with the stored outcome an idempotent retry replays | yes, with the conversation | the idempotency period (24 h), then pruned |
| `app_graph` checkpoints | Workflow state, one thread per run | yes, with the conversation | finished threads pruned at once; orphans after 24 h |
| `app_auth.sessions` | Sign-in state: token hash, times, user agent, address hash | no; `POST /api/logout` ends one | 30 days after expiry or revocation |
| `app_auth.login_attempts` | Failed and successful attempts, for throttling | no | 30 days |
| `app_auth.identities` | Linked single-sign-on identities | no | while the account exists |
| `app_meta.query_audit` | The security record: hashes, codes, counts, versions, timings | **no** (see below) | 400 days |
| `app_ingest.quarantine` | Invalid ingested events, for diagnosis and resubmission | n/a (business data) | 90 days |
| In-process caches | Vocabularies and entity indexes, keyed by dataset and scope | n/a (no personal data) | until the next dataset generation |
| Telemetry | Spans and metrics, redacted before export | n/a | the collector's own retention. Nothing protected is exported ([OBSERVABILITY.md](OBSERVABILITY.md)) |

Every period is configuration (`PAC_CONVERSATION_RETENTION_DAYS`,
`PAC_AUDIT_RETENTION_DAYS`, `PAC_SESSION_RECORD_RETENTION_DAYS`,
`PAC_LOGIN_ATTEMPT_RETENTION_DAYS`, `PAC_QUARANTINE_RETENTION_DAYS`). **The
defaults are proposals, not an agreed policy.** A deployment sets them from
its own data-protection and audit requirements.

## Export

`GET /api/me/data` returns a JSON download: the profile, every conversation
with its turns, cohorts and their members, clarifications and run records,
sessions (times and user agent only; no token or address hashes), linked
identities, and a summary of audit records.

It is bound to **current access**, like every other read. A conversation
recorded under access the user no longer holds is withheld whole, because
its questions can name accounts from a territory they have left. It is
counted in `withheld_conversations`, so the export does not present itself
as complete when it is not.

## Deletion

- `DELETE /api/conversations/{id}` deletes one conversation.
- `DELETE /api/me/data` deletes all of them.

Each deletion takes the conversation's turns, cohorts and members,
clarifications, runs, and workflow checkpoints with it. Deletion covers
everything the user owns, **whatever access it was recorded under**,
because deleting discloses nothing. Every statement is keyed by the
caller's user id from the session. Another user's conversation returns the
same 404 as one that does not exist. A conversation with a question still
in flight is refused (`409 conversation_busy`) rather than deleted from
under it. A cross-site request is refused by the request guard before any
route runs.

The user stays signed in. Signing out is separate.

### The audit exception

Audit rows are **not** removed by a deletion request. They are the record
of what was accessed, and a deletion request must not be able to erase
them. They hold hashes, codes, counts, versions and timings, with no
question text, SQL or results. The serving role cannot delete them at all:
it has `INSERT` and `SELECT` only. They expire after the audit retention
period, deleted by the jobs container running as the owner.

## Feedback and failure triage

Feedback (`POST /api/feedback`) is owner-scoped like the answer it rates.
Only the asker can give it, and only under their current access. It does
not change behaviour by itself.

`scripts/failure_sample.py`, run as the owner in the jobs container, draws
a random sample of recent failures for triage. A failure here is a request
that ended in an error, a blocking intent gap or a refresh, or an answer
marked "not right". Each record carries:

- the outcome, reason codes and intent gaps;
- the metric, plan and SQL hashes;
- the versions, timings and role.

It carries nothing that identifies or quotes the user: no user id, email,
question text or answer. Comments are the user's own words, so they are
withheld unless the operator passes `--include-comments`.

A confirmed failure goes back into the product as a regression test that a
person writes with an **invented** question. The user's text is never copied
into the repository. Policy is never relaxed to make a complaint go away.
No model is trained on any of it.

## The scheduled job

```bash
# In the jobs container (owner credential):
python3 scripts/prune_state.py              # prune workflow state, then apply retention
python3 scripts/prune_state.py --no-retention
```

The job is idempotent and safe to run on any schedule. It never deletes a
conversation that has a live request, or a thread whose clarification is
still pending. Checkpoint threads are deleted after the database commit. A
failure there leaves only threads whose conversation is gone, and the
orphan pruning removes those on the next run.

## Tested

- export contains the caller's conversations and nobody else's, with
  questions, answers, cohort members and runs, and no token or address
  hashes;
- export withholds a conversation recorded under former access, including
  its question text;
- deleting a conversation, including one paused on a clarification, leaves
  no turns, cohorts, clarifications, runs or checkpoint rows, and keeps the
  audit rows;
- another user's conversation cannot be deleted;
- a conversation with a request in flight is not deleted, by either route;
- deleting everything covers conversations from former access, and the
  user stays signed in;
- a cross-site DELETE is refused;
- retention deletes a stale conversation with its threads, keeps a recent
  one and one with a live request, and deletes audit rows past the period
  while keeping recent ones.

With the owner filter removed from deletion and the access binding removed
from export, 3 of these fail.

## Not covered

- Users are the supplied `users` table, which is the authority for role and
  scope. Deleting an **account** is an administrative action on that table,
  not a self-service route.
- Backups hold deleted data until they expire. Backup retention is set by
  the database deployment, not here.
- The proposed periods have not been reviewed against any regulation or
  contract.
