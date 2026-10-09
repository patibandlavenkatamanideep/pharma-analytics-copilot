# Restricted pilot — go/no-go, 9 October 2026

| Readiness for | Decision | Why |
|---|---|---|
| Infrastructure smoke testing on AWS | **deployed** 9 October: https://staging.pharma-copilot.click, the owner-approved plan, until 16 October; the hosted checks in [AWS_STAGING.md](AWS_STAGING.md) pass, three are outstanding | synthetic data, offline planner, one task, Single-AZ |
| Restricted pilot | **NO-GO** | staging is a review environment only; no real identity provider or feed, no live-model evaluation, no agreed targets |
| Production | **NO-GO** | everything a pilot lacks, plus replicas, failover, hosted telemetry and recovery measured against targets |

No result below is inferred from local test counts; each says where it ran.

**Head.** Branch `codex/release-defects-oct06` on GitHub. Executable candidate
**`a786bda`** (the last commit changing code, tests, configuration, workflow or build
inputs); later commits change documentation, evidence and `.gitleaksignore` only.
Release integration pull request [#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1) was merged by the owner on
9 October (merge commit `6b139a7`); later work reaches `main` by pull request.

**Authorised by the owner (9 October):** commits and normal pushes to the feature
branch; protecting `main` and creating the `staging` environment; preparing pull
requests; AWS onboarding and planning; then **the staging plan and its spend ($40 in
total, until 16 October)**, carried out without asking again: infrastructure, image
publication, deployment and verification. Bedrock spend: **$5** ($3 website allowance,
$2 evaluation with token caps). Merging #1 and approving each publishing run stay the
owner's own actions.

## Status

| | Result | Where it ran | Identity |
|---|---|---|---|
| **Independently verified** | | | |
| CI, all four jobs, on the candidate | passed: `test` (strict suite, security 450, ingestion, offline evaluations, browser journeys), `frontend`, `supply-chain` (audits, secret scan, Terraform lock, fmt, validate, mocked plans), `image` (build, scan, hardened journeys) | GitHub Actions | run [37957713810](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37957713810), `a786bda` |
| Publishing | CI verified on `5536526`; image built, scanned, journeys run, pushed, registry manifest checked | GitHub Actions, owner-approved environment | run 37939037102 (attempt 2), `sha256:bf04b0175a27…` |
| Staging on AWS | deployed from approved, reconciled plans; database tasks exit 0 (full dataset, boundary intact); 13 of 13 live checks over HTTPS | `pac-staging`, us-east-1 | [AWS_STAGING.md](AWS_STAGING.md), `r5-staging-headers-fixed.json` |
| Branch protection on `main` | pull request required; the four checks, from GitHub Actions, up to date; administrators included; no force-push or deletion; conversations resolved; 0 approvals (one maintainer) | GitHub API, read back | `evidence/external.json` |
| `staging` environment | required reviewer the owner; administrators cannot bypass; `main` and `codex/release-*` only | GitHub API, read back | `evidence/external.json` |
| **Reported (local, this machine)** | | | |
| Release chain on the candidate | passed every gate (the same suites as CI, plus the unit tests without a database and the gate's self-test) | macOS arm64, PostgreSQL 16.14, podman (linux/amd64 under emulation) | `r5-rc-a786bda-*.json` |
| Image | built, scanned (8 HIGH base-image advisories, none fixable, none reachable in the deployed configuration), 28 hardened checks | podman, local | config `c30ad1e871e6…`; not a registry digest |
| Sign-out | 4 scenarios under a controlled network: abandoned, applied but unanswered, refused, straight to another user | Chromium, local server | `r5-signout-refused-fixed.json` |
| RDS restricted administrator; verified TLS; proxy trust | pass (emulated administrator; private CA; local proxy) | local | `r5-rds-admin-fixed.json`, `r5-db-tls-verify-full.json`, `r5-proxy-client-address-fixed.json` |
| New-cluster restore; upgrade from `7950e71` | pass | local clusters | `r5-restore-new-cluster-a786bda.json`, `r5-upgrade-compatibility-a786bda.json` |
| Staging plans | 63 resources for the foundation, 73 deployable, +2 with the public sign-in page; 14 configurations planned and refused as designed | mocked provider (also in CI) | `r5-staging-terraform-test-a786bda.json`, `infra/aws-staging/PLAN_INVENTORY.md` |
| Cost | ~$96/month low-cost; ~$213/month during failover tests | AWS public price list, us-east-1 | `infra/aws-staging/cost/ESTIMATE.md` |
| **Blocked** | | | |
| A pull request with a failing check being blocked | the first real one is #1; not yet observed failing | — | — |
| The sign-in page from outside `allowed_cidrs`; proxy trust from two networks; the TLS negative run | not yet run on AWS | — | — |
| Real IdP role mapping; cross-user checks on staging | no IdP registration; no staging | — | — |
| RDS provisioning, ingestion and freshness on AWS | no staging; no feed samples | — | — |
| Live-model accuracy | $5 approved; the account's Anthropic use-case form not yet submitted | — | — |
| Telemetry and alert delivery | no staging; traces would go to a debug exporter only | — | — |
| Managed point-in-time restore; release-pair rollback on AWS; failover | no staging; Single-AZ in the first stage | — | — |

When authorised, every hosted check is recorded with `scripts/record_evidence.py
--require-clean --image <repository>@<digest>` naming the commit, the registry digest,
the Terraform variables (no secret), the account and the region
([AWS_STAGING.md](AWS_STAGING.md), "On AWS"; [STAGING_VERIFICATION.md](STAGING_VERIFICATION.md)).

## Terraform review (9 October)

| Area | Finding | Outcome |
|---|---|---|
| State and secrets | RDS generates the administrator's password; secret containers hold no values; no secret in plain environment; state ignored. **Defect:** the README's `staging.tfvars` and saved plans were not ignored | fixed, reproduced first (`staging-files-not-ignored`) |
| State backend | local unless the S3 backend is enabled | stage 1 of the plan |
| GitHub OIDC trust | audience `sts.amazonaws.com`; subject `repo:…:environment:staging` (exact); the environment exists, reviewer-gated, no administrator bypass; the role pushes images only | **Defect found on AWS:** the role trusted only the legacy subject, and this repository's tokens carry the immutable one; publishing failed (run 37939037102), fixed in `facd0d4` (`github_subject_prefix`; a wildcard refused). Otherwise no defect. Residual: any workflow on an allowed branch can request the environment; the reviewer gates it |
| IAM scope | execution roles split; task roles empty unless Bedrock or Prometheus; publish role ECR-only | no defect |
| Direct application access | tasks accept port 8000 only from the load balancer's group; the load balancer from `allowed_cidrs`, and from everywhere on 443 and 80 when `public_sign_in` (the owner's decision) | no defect; the live site answers through the load balancer only. Browser security headers were missing (found live, added by the load balancer in `a786bda`) |
| Proxy-header trust | trusted range is the VPC; the load balancer appends; uvicorn takes the rightmost untrusted address (guard test) | fixed earlier (`proxy-client-address`) |
| Database TLS | `verify-full` with the pinned RDS bundle everywhere; `rds.force_ssl` | no defect; hosted unverified |
| Migration privileges | the RDS administrator gets only `SET ROLE pac_owner`; migrations run as the owner and need nothing more | no defect |
| Image digest pinning | application and jobs by digest. **Defect:** an unpinned collector was only warned about | fixed, reproduced first (`collector-pin-warning-only`) |
| Image retention | ECR expired every image beyond the last 20, the deployed one included | designed around: `keep-*` images are retained (up to 9,999 of them, confirmed by a lifecycle preview on AWS); a deploy refuses an unprotected digest |
| Publishing | the workflow would publish any dispatched commit and did not tie the pushed image to the tested one | designed around: CI must have passed on the exact commit; the registry manifest is compared with the tested image |
| Teardown and recreate | fixed secret names stay reserved for 7 days; a fixed final-snapshot name blocks a second teardown | designed around: secret generations; an operator-chosen snapshot label. Verified only on AWS (stage 8) |
| Rollback | forward-only migrations; ECS rolls back failed deployments; no earlier release is a rollback target | Residual: whether ECS's automatic rollback needs the previous task-definition revision active, which Terraform deregisters, is unverified |

Defects found by hosted CI on the exact head, each reproduced and fixed:
`ci-ledger-check-shallow-checkout`, `terraform-lock-single-platform`,
`signout-before-revocation` (and, testing failed sign-outs,
`signout-unconfirmed-presented-as-done`).

## Unresolved risks and owners

One maintainer: each item is owned by **Venkata Manideep (repository owner)** until
delegated; where independence is required, a second named person is needed.

| Risk | Owner | Next action |
|---|---|---|
| Three hosted checks outstanding | Venkata Manideep | the sign-in page from a phone on mobile data; a second network for proxy trust; the TLS negative run |
| Staging must end by 16 October | Venkata Manideep | a walkthrough, then teardown (STAGING_PLAN.md, stage 9) |
| No live-model evaluation of prompt 2.3.0 | Venkata Manideep | submit the Anthropic use-case form in `pac-staging`; the $2 evaluation follows |
| No independent holdout | an independent author — **unassigned** | someone who has not seen the development sets (`evals/packet/`) |
| No real identity provider | Venkata Manideep with the IdP administrator — **unassigned** | client registration and role mapping |
| No real feed | the source-system owner — **unassigned** | samples, control totals, source contract |
| Vulnerability triage expires 2026-11-07 unsigned | Venkata Manideep (release owner until named) | sign or re-triage |
| No targets | Venkata Manideep with product and compliance owners | service, freshness, RTO/RPO, audit mode, retention |
| Lifecycle and rollback residuals | Venkata Manideep | stage 8 of the plan |
| Browser failure artifacts not yet exercised | Venkata Manideep | the next failing journey in CI shows whether they suffice |
