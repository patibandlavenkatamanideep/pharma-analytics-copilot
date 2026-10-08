# Rollback and recovery decision record

**Status:** decided 7 October 2026 for this candidate, locally verified; no deployment
exists, so nothing here has been exercised in a hosted environment.

**Question.** If this candidate misbehaves after it is deployed, how is service
restored: by running a previous image against the upgraded database (rollback), by a
new image built from a fix (forward fix), or by restoring a backup?

## Decision

1. **No code rollback.** No previous image is a safe target. The previous executable
   candidate, `7950e71`, lacks twelve of the fixes in `evidence/ledger.json`, among them
   an access defect (`territory-name-scope`: a geography reusing a territory or region
   name widened a scope bound by name), two that report wrong numbers
   (`segment-share-population`, `classification-by-elimination`) and the restore
   procedure below; every earlier candidate lacks more. Rolling back would
   reintroduce a known access flaw, which the operating rules forbid.
2. **Forward fix is the default.** A fix is committed on the release branch, goes
   through the same gates, and is deployed as a new image. Migrations only move forward
   and converge (they are re-runnable); a fix that needs a schema change adds a
   migration, never edits one.
3. **Restore is for lost or damaged data**, not for undoing a release: the latest backup
   of the *upgraded* database, restored into a new cluster by the RUNBOOK §8 procedure,
   served by this candidate (or its forward fix). Restoring a pre-upgrade backup to run a
   previous image combines a data loss (everything since that backup) with the code
   rollback refused above.
4. **Containment while a fix is prepared**, without changing code: disable single
   sign-on (`PAC_OIDC_ENABLED=false`, password sign-in remains), take a replica out of the
   load balancer, or stop the jobs container (no new publication; readers keep the last
   published generation). Each is a configuration change on the reviewed image.

## Compatibility restrictions (measured)

`evidence/probes/upgrade_compatibility.py` (`r5-upgrade-compatibility.json`): the
previous release's source (`7950e71`, run as processes, not its image) created a
database with live state; this candidate's migrations upgraded it, twice, converging
(0.3 s each on seed data).

| Check on the upgraded database | This candidate | Previous release's code |
|---|---|---|
| A session signed in before the upgrade | valid | valid |
| Password sign-in | works | works |
| An answer committed under an idempotency key, replayed | identical | identical |
| A clarification paused before the upgrade, answered | resumes | resumes |
| A run whose process was killed before the upgrade | taken over after its lease, committed once | (already committed) replayed |
| A RAM's rows; another territory | own rows; denied | own rows; denied |
| Revenue for an executive without pricing | no figure | no figure |
| Sign-in methods with SSO disabled | password only; OIDC start 404 | password only; OIDC start 404 |
| Audit rows it writes | with `run_id` and `audit_mode` | **without** them (024 columns are nullable) |
| Full data load | succeeds | **fails**: `NotNullViolation` on `product_classification.authority` (migration 023); the published dataset is left unchanged |

So the previous code *could* serve the upgraded schema, but it could not load data, its
audit rows lose the run linkage strict audit relies on, and it would answer with the
defects listed above. The measurement documents the restriction; it does not make the
previous release a candidate.

## Tested procedures

- **Forward upgrade with live state**: above (`r5-upgrade-compatibility.json`).
- **Restore into a new cluster**: `scripts/restore_drill.py --new-cluster`
  (`r5-restore-new-cluster-fixed.json`), after the documented procedure was found to
  lose all conversation state (`r5-restore-new-cluster-reproduced.json`; fixed
  `6ced978`).

## Not established

A deployed rollback or roll-forward; image pull and orchestration; a managed database's
snapshot restore or point-in-time recovery; recovery from off-host backup storage; the
time to build, scan and ship a forward fix.
