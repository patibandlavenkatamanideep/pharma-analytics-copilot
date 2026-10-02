# Staging verification

What a staging deployment of this branch has to prove before a release can
be called ready, as steps with pass criteria. **None of it has been run.**
No staging environment exists for this branch, and deploying one is not
authorized. Everything here is locally tested with stand-ins: an
in-process IdP, a local OTLP receiver, threads as workers, and a podman
image on this machine. Those stand-ins are not proof for a real
environment ([RELEASE_EVIDENCE.md](RELEASE_EVIDENCE.md#blocked-what-needs-an-external-input)).

Record every step with `scripts/record_evidence.py --require-clean --image
<ref>`. A staging result counts only for the image it ran against, named
by digest.

## Before starting

| Needed | Why |
|---|---|
| A staging host or cluster, and authorization to deploy there | Nothing is deployed for this branch |
| The image CI built and scanned from the release commit, by **digest** | A tag names whatever was built last. Compare `/health`'s `release` with the image's `org.opencontainers.image.revision` label, and the running image's id with CI's |
| A PostgreSQL 16 instance provisioned with `scripts/bootstrap_db.py` and loaded through the jobs container | The serving container must not hold the owner credential |
| An OIDC client registration: issuer, client id and secret, and redirect URI `https://<host>/api/auth/oidc/callback` | Real single sign-on |
| An OTLP/HTTP collector, a metrics backend, and the alert rules from [OBSERVABILITY.md](OBSERVABILITY.md#alerts) loaded | Telemetry delivery and alerting |
| Agreed availability and recovery targets: SLOs, RTO, RPO | Several pass criteria below are targets nobody has set yet |

## 1. Identity: the deployed image is the tested one

```bash
curl -s https://<host>/health                    # release == the commit CI built
podman image inspect <image>@<digest> --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Pass if the release, the revision label and CI's built-and-scanned digest
all name the same commit.

## 2. Real single sign-on (review R1)

Follow [RUNBOOK.md, "Verifying with a real provider"](RUNBOOK.md#enabling-single-sign-on):
a linked user signs in, the binding cookie is `__Host-pac_oidc` with Secure,
HttpOnly, SameSite=Lax and Path=/, a callback carried to a second browser
gets `400 browser_mismatch`, cancellation and expiry are refused, a
disabled account gets `403 disabled`, and key rotation is picked up.

Pass if every step behaves as written, with the provider's MFA policy
applied as configured.

## 3. Permission changes take effect at once

As an administrator:

1. Move a RAM to another territory (`users.territory_name`). Their next
   question is answered for the new territory. Their earlier conversations
   are withheld, not shown under the old scope.
2. Remove a Director's `can_view_wac`. No answer, cell, note or SQL shows a
   price on their next question, and replaying an earlier priced answer is
   refused (`403 access_changed`).
3. Disable an account. Its live sessions stop working on the next request,
   for password and SSO sign-in alike.

Pass if each change applies on the next request, with no restart.

## 4. Per-user limits across workers and replicas (review R2)

With at least two replicas behind the load balancer and the defaults (20
per minute, 300 per hour, 2 concurrent per user):

1. From one user, send 3 concurrent questions spread across replicas. At
   most 2 are answered and the rest get `429 rate_limited`.
2. Make a request fail (stop the database for its duration, or use a
   question that times out), then retry it with the same `Idempotency-Key`
   until refused. Each retry counts: the 21st attempt in a minute is `429`.
3. Replay a committed answer with its key after the allowance is spent. It
   is returned (`replayed: true`), not refused.

Pass if the limits hold across replicas: they are counted in the database,
not per process.

## 5. Restart and checkpoint recovery

1. Start a question that will ask for clarification. Restart every app
   replica (`docker compose restart app`, or roll the deployment). Answer
   the clarification. The conversation resumes from its PostgreSQL
   checkpoint.
2. Kill a replica mid-answer (`kill -9`). Retrying with the same key
   reclaims the abandoned run after its lease expires (at most 120 s), and
   exactly one outcome is committed.
3. Send SIGTERM during an answer. It completes within the 65 s graceful
   window (`scripts/drain_check.py` measures this locally).

Pass if no conversation is lost or duplicated, and each key has at most one
committed outcome.

## 6. Overload

Run `scripts/load_test.py` against staging with the profile in
[CAPACITY.md](CAPACITY.md), at the client count where the deployment's
total admission limit is exceeded. That limit is the per-worker limit ×
workers × replicas.

Pass if the excess gets `503 overloaded` with `Retry-After` quickly rather
than timing out, and answered requests meet the latency target, once one
is agreed.

## 7. Ingestion under load (reviews R4 and R5)

1. With the load test at the planned concurrency, ingest an in-week batch,
   then a new-week batch, through the jobs container (`scripts/ingest.py`).
   Readers keep answering. Requests planned before a publication and run
   after it are answered `refresh`, never with mixed data.
2. Send a batch with a NaN price and one with a malformed timestamp. The
   NaN is quarantined (`non_finite_number`), the bad timestamp is
   quarantined, and nothing non-finite reaches `sales`.
3. Send a batch with `"declared_pack_units": NaN`. It is rejected whole
   (`invalid_envelope`), and published data is unchanged.
4. The batches, events, quarantine and the `pac.ingest` span appear in the
   collector, from the jobs process.

Pass if all four hold.

## 8. Stopped-feed alerts (review R4)

1. Let a healthy batch publish, then stop the feed.
   `pac.ingest.since_success` keeps rising, and the "Missed ingestion run"
   alert fires after its threshold. Set the threshold to the feed's
   schedule plus a margin.
2. Stop the collector, or every app replica. "Freshness not reported"
   fires.
3. Run `scripts/ingest.py --check-freshness --max-since-success <limit>`
   from the scheduler. It exits 3 with `missed_run`.

Pass if each alert fires, and clears once the feed resumes.

## 9. Logs

Collect the app's stderr into the log store.

Pass if every line is one JSON object, no line holds a question, an email,
SQL, a query string or a client address, and a user-reported `X-Request-ID`
finds that request's lines, including its `request_id` and `run_id`.

## 10. Rollback and recovery, against agreed targets

1. Roll back to the previous image digest. Migrations are additive, so the
   previous image runs on the migrated schema. Confirm with the previous
   release's own image journeys.
2. Restore the latest backup into a **new cluster**, with roles first
   ([RUNBOOK.md §8](RUNBOOK.md)). Repoint staging at it, and time the
   whole procedure from the decision to restore to `/ready`.
3. If point-in-time recovery is required, restore to a named time and
   confirm the data up to it.

Pass if the measured recovery time and data loss are within the agreed RTO
and RPO. Until those are agreed, record the measurements and do not judge
them.
