# Restricted pilot — go/no-go, 9 October 2026

| Readiness for | Decision | Why |
|---|---|---|
| Infrastructure smoke testing on AWS | **not started**: ready to begin once the inputs in [STAGING_PLAN.md](STAGING_PLAN.md) are supplied and that plan and its spend are approved | no AWS access on this machine; nothing billable is created before approval |
| Restricted pilot | **NO-GO** | no staging environment, no real identity provider or feed, no live-model evaluation, no agreed targets |
| Production | **NO-GO** | everything a pilot lacks, plus replicas, failover, hosted telemetry and recovery measured against targets |

No result below is inferred from local test counts; each says where it ran.

**Head.** Branch `codex/release-defects-oct06` on GitHub. Executable candidate
**`e101db3`** (the last commit changing code, tests, configuration, workflow or build
inputs); later commits change documentation, evidence and `.gitleaksignore` only.
Release integration pull request [#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1)
is open for review; it is not merged.

**Authorised by the owner (9 October):** commits and normal pushes to the feature
branch; protecting `main` and creating the `staging` environment; preparing pull
requests; AWS onboarding, read-only discovery and planning once the owner supplies the
account context and access. **Not yet authorised:** creating billable resources (needs
the approved plan), publishing to a registry and deploying (part of that plan), and any
Bedrock spend (its own token and dollar budget).

## Status

| | Result | Where it ran | Identity |
|---|---|---|---|
| **Independently verified** | | | |
| CI, all four jobs, on the candidate | passed: `test` (strict suite 2240, security 436, ingestion 177, offline evaluations, 15 browser journeys), `frontend`, `supply-chain` (audits, secret scan, Terraform lock, fmt, validate, mocked plans), `image` (build, scan, hardened journeys) | GitHub Actions | run [37869897240](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37869897240), `e101db3` |
| Branch protection on `main` | pull request required; the four checks, from GitHub Actions, up to date; administrators included; no force-push or deletion; conversations resolved; 0 approvals (one maintainer) | GitHub API, read back | `evidence/external.json` |
| `staging` environment | required reviewer the owner; administrators cannot bypass; `main` and `codex/release-*` only | GitHub API, read back | `evidence/external.json` |
| **Reported (local, this machine)** | | | |
| Release chain on the candidate | passed every gate (the same suites as CI, plus 847 unit tests without a database and the gate's self-test) | macOS arm64, PostgreSQL 16.14, podman (linux/amd64 under emulation) | `r5-rc-e101db3-*.json` |
| Image | built, scanned (8 HIGH base-image advisories, none fixable, none reachable in the deployed configuration), 28 hardened checks | podman, local | config `68178125d860…`; not a registry digest |
| Sign-out | 4 scenarios under a controlled network: abandoned, applied but unanswered, refused, straight to another user | Chromium, local server | `r5-signout-refused-fixed.json` |
| RDS restricted administrator; verified TLS; proxy trust | pass (emulated administrator; private CA; local proxy) | local | `r5-rds-admin-fixed.json`, `r5-db-tls-verify-full.json`, `r5-proxy-client-address-fixed.json` |
| New-cluster restore; upgrade from `7950e71` | pass | local clusters | `r5-restore-new-cluster-e101db3.json`, `r5-upgrade-compatibility-e101db3.json` |
| Staging plans | 62 resources for the foundation, 70 deployable; 10 configurations planned and refused as designed | mocked provider (also in CI) | `r5-staging-terraform-test-e101db3.json`, `infra/aws-staging/PLAN_INVENTORY.md` |
| Cost | ~$96/month low-cost; ~$213/month during failover tests | AWS public price list, us-east-1 | `infra/aws-staging/cost/ESTIMATE.md` |
| **Blocked** | | | |
| A pull request with a failing check being blocked | the first real one is #1; not yet observed failing | — | — |
| Read-only discovery, `terraform plan`, apply | no AWS session; inputs and plan approval needed | — | — |
| Registry digest; deployment; URL | publishing needs #1 merged and the plan approved | — | — |
| Real IdP role mapping; cross-user checks on staging | no IdP registration; no staging | — | — |
| RDS provisioning, ingestion and freshness on AWS | no staging; no feed samples | — | — |
| Live-model accuracy | no Bedrock budget | — | — |
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
| GitHub OIDC trust | audience `sts.amazonaws.com`; subject `repo:…:environment:staging` (exact); the environment exists, reviewer-gated, no administrator bypass; the role pushes images only | no defect. Residual: any workflow on an allowed branch can request the environment; the reviewer gates it |
| IAM scope | execution roles split; task roles empty unless Bedrock or Prometheus; publish role ECR-only | no defect |
| Direct application access | tasks accept port 8000 only from the load balancer's group; the load balancer only from `allowed_cidrs` | no defect; shown on AWS in stage 7 |
| Proxy-header trust | trusted range is the VPC; the load balancer appends; uvicorn takes the rightmost untrusted address (guard test) | fixed earlier (`proxy-client-address`) |
| Database TLS | `verify-full` with the pinned RDS bundle everywhere; `rds.force_ssl` | no defect; hosted unverified |
| Migration privileges | the RDS administrator gets only `SET ROLE pac_owner`; migrations run as the owner and need nothing more | no defect |
| Image digest pinning | application and jobs by digest. **Defect:** an unpinned collector was only warned about | fixed, reproduced first (`collector-pin-warning-only`) |
| Image retention | ECR expired every image beyond the last 20, the deployed one included | designed around: `keep-*` images never expire; a deploy refuses an unprotected digest |
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
| Nothing verified on AWS | Venkata Manideep | supply the inputs in STAGING_PLAN.md; approve the plan and spend |
| `publish-staging` cannot run until #1 is merged | Venkata Manideep | review #1 once its checks pass |
| No live-model evaluation of prompt 2.3.0 | Venkata Manideep (budget) | set a Bedrock token and dollar cap for a smoke run |
| No independent holdout | an independent author — **unassigned** | someone who has not seen the development sets (`evals/packet/`) |
| No real identity provider | Venkata Manideep with the IdP administrator — **unassigned** | client registration and role mapping |
| No real feed | the source-system owner — **unassigned** | samples, control totals, source contract |
| Vulnerability triage expires 2026-11-07 unsigned | Venkata Manideep (release owner until named) | sign or re-triage |
| No targets | Venkata Manideep with product and compliance owners | service, freshness, RTO/RPO, audit mode, retention |
| Lifecycle and rollback residuals | Venkata Manideep | stage 8 of the plan |
| Browser failure artifacts not yet exercised | Venkata Manideep | the next failing journey in CI shows whether they suffice |
