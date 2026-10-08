# External verification plan

What has to happen outside this machine before a pilot, in order, with the exact
commands and checks, and the inputs each step needs. Nothing here has been run: each
step needs an authorization that this repository's work does not have. Prepared on
8 October 2026 for the executable candidate `a9de92e` ([RELEASE_HANDOFF.md](RELEASE_HANDOFF.md)).

## 1. Push, pull request and hosted CI

**Needs:** authorization to push to `github` (`patibandlavenkatamanideep/pharma-analytics-copilot`).

```bash
git fetch github
git merge-base --is-ancestor github/codex/release-defects-oct06 HEAD   # must succeed: fast-forward only
git push github codex/release-defects-oct06                             # never --force
gh run list --repo patibandlavenkatamanideep/pharma-analytics-copilot \
  --branch codex/release-defects-oct06 --limit 1                        # the run for HEAD's SHA
gh run watch <run-id> --repo patibandlavenkatamanideep/pharma-analytics-copilot --exit-status
gh run view <run-id> --repo patibandlavenkatamanideep/pharma-analytics-copilot --log > ci-<sha>.log
gh run download <run-id> --repo patibandlavenkatamanideep/pharma-analytics-copilot -n eval-run
```

Pass: the run's `headSha` is the pushed HEAD; jobs `test`, `frontend`, `supply-chain` and
`image` all succeed; the `test` log shows `release gate satisfied` for the security,
ingestion and **full-suite** steps (the last is new since `03316f3` and has never run on a
hosted runner); the `image` job shows "It carries no setuid or setgid file" (new since
`42e9090`); the trivy-action scan passes. (`42e9090`'s first version of the setuid step would have
failed: it searched as `appuser`; `a9de92e` searches as root.) Record the run in `evidence/external.json` (run
id, head SHA, conclusion, per-job outcome), the image job's image ID and revision label,
and keep the log and the `eval-run` artifact with their SHA-256.

A green run on `69c62de` (run 37653591687) says nothing about this candidate.

**Then, with separate authorization**, a draft pull request — never a merge:

```bash
gh pr create --repo patibandlavenkatamanideep/pharma-analytics-copilot --draft \
  --base main --head codex/release-defects-oct06 --title "Release qualification: <sha>"
```

## 2. Branch protection on `main`

**Found (8 October 2026, read-only API):** `main` is **not protected** and has no
rulesets. Nothing enforces the checks above before a merge.

**Needs:** repository-settings authorization. Then require, on `main`: a pull request; the
status checks `test`, `frontend`, `supply-chain`, `image` (workflow `CI`), up to date with
`main`; no force pushes; no deletions; administrators included. One approving review
once a second maintainer exists (with one, a required review blocks every merge or needs a
bypass). The exact `gh api` commands, and the `staging` environment the publishing
workflow needs, are prepared in [AWS_STAGING.md](AWS_STAGING.md) ("Blocked actions").
Verify by opening a pull request with a failing check and confirming merge is blocked.

## 3. The release image

CI builds the image with `push: false`; nothing is published. A release needs:

1. **Authorization and a registry.** Prepared for Amazon ECR:
   `.github/workflows/publish-staging.yml` (manual, environment `staging`, GitHub OIDC to a
   push-only role from `infra/aws-staging`); it builds, scans, runs the image journeys,
   pushes and reports the registry digest. Never run.
2. Build once, from the pushed commit, for `linux/amd64`, and push; record the
   **registry manifest digest** (and the index digest if multi-platform). The local image
   ID (`b6d529719b31…`) will not match a CI-built one: another builder, another time, and the
   bases resolve by tag.
3. Scan **that digest** (trivy with its database date; complete counts), re-triage
   against [VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md), run `scripts/image_smoke.sh`
   with `PAC_SMOKE_IMAGE=<registry>/<name>@sha256:<digest>`.
4. Deploy by digest only. Proposed before then: pin `python:3.13-slim` and `node:24-slim`
   by digest in the `Dockerfile`, so the commit determines the bases.

## 4. Live model evaluation

**Needs:** AWS credentials with Bedrock access to the configured model, the contracted
per-million-token rates (`PAC_LLM_INPUT_USD_PER_MTOK`, `PAC_LLM_OUTPUT_USD_PER_MTOK`), and
an approved cap for each run. Prompt **2.3.0**, fingerprint `5dd66431f2ba8230`, planner
contract 2.1.0; recorded in every run.

| Run | Command | Token caps (input / output) | Pass criterion |
|---|---|---|---|
| Smoke | `python3 scripts/run_evals.py --provider bedrock --questions evals/questions.yaml --smoke --max-input-tokens 150000 --max-output-tokens 70000` | 150,000 / 70,000 | no bound violation; every call's usage reported or charged its reservation |
| Regression sets | the same without `--smoke`, for `questions.yaml`, then `holdout.yaml` and `holdout2.yaml` (spent: regression, not accuracy) | about 300,000 / 15,000 for the three | known behaviour holds; k-07 included |
| Fresh holdout | written by someone who has not seen the development sets (`evals/packet/AUTHORING.md`, `PROTOCOL.md`), `status: holdout`, frozen with `scripts/freeze_holdout.py`, `evals/frozen.json` committed **before** the first run | sized from the smoke run's measured cost | reported once, by category: correct answers against the independent oracle, unsafe answers, correct refusals and clarifications, wrong answers, failures; latency p50/p95; tokens and unknown usage; cost |

Dollar ceiling for each cap: input cap × input rate / 10⁶ + output cap × output rate /
10⁶. Keep every run record, including failures and unknown usage. None of the offline
or EXPLAIN-based results is model accuracy.

## 5. Single sign-on with a real identity provider

**Needs:** a client registration: issuer exactly as published, client ID (and secret for
a confidential client), redirect URI `https://<host>/api/auth/oidc/callback`, signing
algorithms `RS256` or `ES256`. Accounts linked by `(issuer, subject)` by an administrator.
Checks ([RUNBOOK.md](RUNBOOK.md), "Verifying with a real provider";
[STAGING_VERIFICATION.md](STAGING_VERIFICATION.md) §2): a same-browser sign-in; a callback
from another browser refused (`browser_mismatch`); an expired or replayed state refused;
a revoked or unlinked identity refused; disabling SSO (`PAC_OIDC_ENABLED=false`) leaves
password sign-in working and the OIDC endpoints refused; issued sessions revoked through
the administrator procedure.

## 6. A real source feed

**Needs:** from the source owner, the items in [REAL_FEED_ACCEPTANCE.md](REAL_FEED_ACCEPTANCE.md):
representative records, control totals, classification provenance, source keys,
deletion semantics and the schedule. Run the onboarding and readiness report on it
(`scripts/readiness_report.py`), then replay, a correction, a deletion, late data, a
rejected batch and a stopped feed through `scripts/ingest.py`, each reconciled against the
ledger, and freshness against the agreed schedule.

## 7. Staging

**Prepared for AWS:** [infra/aws-staging](../infra/aws-staging/README.md) (ECS Fargate, RDS
PostgreSQL 16, HTTPS load balancer, optional Amazon Managed Service for Prometheus), its
cost estimate, and the status of every check in [AWS_STAGING.md](AWS_STAGING.md).

**Needs:** an AWS account and region, authorization to plan and apply, then to deploy the
digest from step 3, and the targets in [TARGETS_DECISION.md](TARGETS_DECISION.md) agreed.
Then
[STAGING_VERIFICATION.md](STAGING_VERIFICATION.md) §1–10: identity of the deployed image,
SSO, permission changes, per-user limits across two replicas, restart and checkpoint
recovery, overload, ingestion under load, stopped-feed alerts, logs, and recovery: a
restore into a fresh database server by RUNBOOK §8, timed against the agreed RTO, its
cutoff against the RPO. Deploy with `no-new-privileges`, all capabilities dropped and a
read-only root filesystem ([VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md)). A
representative soak, then a forward-fix deployment rehearsed (no rollback to an earlier
image: [ROLLBACK_DECISION.md](ROLLBACK_DECISION.md)).

## Inputs requested

| Input | From | Unblocks |
|---|---|---|
| Authorization to push the branch; later, a draft pull request | repository owner | §1 |
| Repository-settings authorization for branch protection | repository owner | §2 |
| A registry and authorization to publish | repository owner | §3 |
| AWS Bedrock credentials, contracted rates, approved caps | budget owner | §4 smoke and regression |
| An independent author for the fresh holdout | evaluation owner | §4 holdout |
| An IdP client registration | identity owner | §5 |
| Feed samples, control totals and the source contract | source-system owner | §6 |
| An AWS account and region (residency), a monthly budget and alert addresses, a hostname and DNS zone, the testers' networks, and authorization to plan, apply and deploy | platform and budget owners | §3, §7 |
| Targets, audit mode, retention and residency decisions; a named release owner for the vulnerability triage | product, data and compliance owners | §7, and the triage's expiry |
