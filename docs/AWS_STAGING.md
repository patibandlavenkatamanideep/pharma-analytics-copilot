# AWS staging — status

**Status as of 9 October 2026.** Candidate `e101db3` on `codex/release-defects-oct06`,
pushed to GitHub. Staging is **prepared, not deployed**: no AWS account access exists on
this machine, and creating billable resources waits for an approved plan
([STAGING_PLAN.md](STAGING_PLAN.md)). Nothing below was run on AWS; local results are
named as local. Neither a restricted pilot nor production readiness can be claimed
([PILOT_DECISION.md](PILOT_DECISION.md)).

## Head, CI and branch protection

| Item | State | Identity |
|---|---|---|
| Branch on GitHub | `the branch head, after the candidate: evidence and documentation only` (pushed normally; never forced) | `codex/release-defects-oct06` |
| Executable candidate | `e101db3` | full local release chain: `r5-rc-e101db3-*.json` |
| Hosted CI on the candidate | all four jobs passed | run [37869897240](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37869897240) |
| `main` | `c8aab5b`, protected (read back 9 October): pull request required; checks `test`, `frontend`, `supply-chain`, `image` bound to GitHub Actions (app 15368) and up to date; administrators included; no force-push or deletion; conversations resolved; 0 approvals (one maintainer) | `evidence/external.json` |
| `staging` environment | required reviewer the owner; administrators cannot bypass; deployments from `main` and `codex/release-*` only | `evidence/external.json` |
| Registered workflows | `CI` only: `publish-staging` can be dispatched once its file is on `main` | release integration pull request [#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1), open for review |

Hosted CI on the way to the candidate — each failure was reproduced and fixed, none
retried away:

| Run | Commit | Result | What it showed |
|---|---|---|---|
| 37865432807 | `e6cb251` | failure | `test`: the ledger check needs the whole history; CI checked out one commit |
| 37866339109 | `811713c` | failure | `supply-chain`: the provider lock held a package hash for the Mac only |
| 37866607406 | `f874b89` | failure | `supply-chain` as above; `test` passed with the whole history |
| 37866723060 | `aea40b2` | failure | `test`: a browser journey found the previous user signed in after Sign out |
| 37868427618 | `8157017` | success | first sign-out fix |
| 37869897240 | `e101db3` | success | the candidate: all four jobs, the Terraform step included |

**History.** On 8 October this document said GitHub's copy ended at `69c62de`, the
last hosted run was 37653591687 and `main` had no protection; all three were true then.

## Infrastructure as code

Terraform, the tool the repository already used (`infra/terraform`, the earlier EC2
demo), in a separate module, [infra/aws-staging](../infra/aws-staging/README.md):
inventory, owner inputs, state, create/update/rollback/failover/teardown order and
cleanup are in its README; the plan for approval is [STAGING_PLAN.md](STAGING_PLAN.md).

| Check | Result | Where | Record |
|---|---|---|---|
| Lock read-only, `fmt`, `validate`, mocked plans | passed | CI (`supply-chain`) on `e101db3` | run 37869897240 |
| 10 mocked plans: the foundation without an image (62 resources), deployable at zero tasks (70), NAT, failover; refusals of a world-open CIDR, a tag for a digest, SSO without a client id, an unpinned collector and an image without a `keep-` tag; secret generations, the snapshot label and the retention rule order | passed | local | `r5-staging-terraform-test-e101db3.json` |
| Every address each configuration would create | generated | local | `infra/aws-staging/PLAN_INVENTORY.md` |
| `trivy config` 0.58.1 | only accepted findings (1 HIGH: internet-facing load balancer, limited to `allowed_cidrs`; 1 MEDIUM: IAM database authentication off; LOW: AWS-managed keys, no Container Insights or Performance Insights) | local, `4c90704` | `r5-staging-trivy-config.json` |
| Security properties from source (15 tests) | passed | CI and local | `r5-rc-e101db3-pytest.json` |
| **`terraform plan` against an account; apply** | **blocked**: no AWS session; plan approval needed | — | — |

Lifecycle, designed before the first teardown and verified only on AWS (STAGING_PLAN.md,
stage 8): ECR never expires `keep-*` images and a deploy refuses an unprotected digest;
secret names carry a generation, so a recreate inside the 7-day recovery window restores
and imports or moves to the next generation; the final snapshot is named by an
operator-chosen label.

## Cost

[infra/aws-staging/cost/ESTIMATE.md](../infra/aws-staging/cost/ESTIMATE.md), from AWS's
public price list for us-east-1 (13 offers dated 2026-09-11 to 2026-10-07): about **$96
a month ($3.15 a day)** for low-cost staging (one task, Single-AZ, offline planner, no
hosted telemetry) and about **$213** while replica and failover tests run. It is
refreshed for the chosen region before approval. Model calls cost about $30 per 1,000
questions at the `us.` profile's list rates. Budgets alert; they do not cap.

## Image, publishing, deployment identity, URL

| Item | State |
|---|---|
| Image | built locally from `e101db3` for linux/amd64: config `68178125d860…` (podman, under emulation). A local image ID is not a registry digest |
| Publishing | `publish-staging.yml`: publishes only a commit whose four CI checks passed; builds one single-platform manifest; scans it and runs the hardened journeys on it; checks the registry's manifest is that image; records source commit, tested image and registry digest. **Cannot be dispatched** until its file is on `main` (#1); never run |
| Registry digest | **none** |
| Retention | images tagged `keep-*` (the deployed and the recovery image) are never expired; others beyond the last 20 are |
| Publishing identity | prepared: role `pac-staging-github-publish`, trusted only for this repository's `staging` environment; pushes images and nothing else. Not created |
| Deploying identity | the owner's IAM Identity Center session applying a reviewed plan. Not exercised |
| URL | **none** |

## Integrations

Local results are named as local; "hosted" is filled only by a run on AWS.

| Requirement | Local evidence | Result | Hosted |
|---|---|---|---|
| RDS with a restricted administrator | as a role with only CREATEROLE and CREATEDB: failed before the fix (`739957a`), passed after (`4ab8fd7`): provisioning, boundary, an answer, restore into a new cluster | pass (emulated) | blocked |
| No owner, superuser or BYPASSRLS for the serving application | the boundary check after provisioning and restore; the serving task gets only the serving roles' secrets | pass | blocked |
| Certificate-verified TLS | a TLS-only cluster with its own CA; another CA and plaintext refused (`4a234e2`); the image pins the RDS bundle (checked by its smoke test) | pass (private CA) | blocked |
| Non-root, capabilities dropped, read-only root | image smoke at `e101db3`, 28 checks: read-only root without tmpfs, every capability dropped, no-new-privileges, one writable volume, no setuid or setgid file | pass | blocked (Fargate volume ownership unverified) |
| Proxy trust | untrusted, 20 failures from other clients refuse a user's password (429); trusted, accepted (`128c558`); a forged `X-Forwarded-For` cannot choose the client (guard test) | pass (local proxy) | blocked |
| Cookies and sign-out | Secure, HttpOnly, SameSite=Lax over HTTPS; sign-out shows the sign-in form only once the server has ended the session, never presents an unconfirmed sign-out as done, and leaves no figure, question or conversation id of the first user (15 journeys, `7a1a2bb`) | pass (local) | blocked |
| OIDC | the security suite against an in-process provider | pass (local) | blocked: no registration with a real provider |
| Model (Bedrock) | offline planner only | not run | blocked: no budget |
| Metrics and alerts | local collector and Prometheus; 588 series per process measured | pass (local) | blocked; traces go only to a debug exporter |

## On AWS, in addition to STAGING_VERIFICATION.md

[STAGING_VERIFICATION.md](STAGING_VERIFICATION.md) §1–10 apply unchanged. On this
stack, each local proof above becomes a hosted one as follows; record each with
`scripts/record_evidence.py --require-clean --image <repository>@<digest>`.

| Proof | Hosted check | Pass |
|---|---|---|
| Restricted administrator | the `bootstrap` task, as the RDS master user (no superuser), then `boundary` | both exit 0 |
| Verified TLS | the same `bootstrap` run: it connects with `sslmode=verify-full` against the image's pinned bundle, and `rds.force_ssl` refuses plaintext | exit 0; a run with another `PGSSLROOTCERT` fails |
| Hardened runtime | `aws ecs describe-task-definition --task-definition pac-staging-app --query 'taskDefinition.containerDefinitions[].[user,readonlyRootFilesystem,linuxParameters.capabilities.drop]'`; `load-full` writes its one volume | `10001`, `true`, `["ALL"]`; exit 0 |
| Proxy trust | from tester network A, 20 failed sign-ins with a throwaway address, some with a forged `X-Forwarded-For`; then a test user from network B | A refused (429) even with the forged header; B signs in (200) |
| Cookies | `curl -si https://<hostname>/api/login -H 'content-type: application/json' -d @login.json \| grep -i '^set-cookie'`, then the same for `/api/logout` with that cookie | `Secure; HttpOnly; SameSite=lax; Path=/` on both |
| OIDC | STAGING_VERIFICATION.md §2 with the registered client | as written there |


## SUID/SGID and the eight Debian advisories

The setuid/setgid assertion is kept in the image smoke and CI's image job. The image from
`e101db3` has none. Its unfiltered scan (trivy 0.58.1, database of 2026-10-08 19:05Z)
matches `e551650`'s exactly: 164 findings, the same 8 HIGH advisories, none fixable in
Debian, none in Python packages, each reviewed against the deployed configuration in
[VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md).

## Pass / fail / blocked

| Gate | Status |
|---|---|
| Local release chain on the candidate | **pass** |
| Hosted CI on the candidate | **pass** (run 37869897240) |
| Branch protection and the `staging` environment | **pass** (applied and read back); a blocked failing pull request not yet observed |
| Release integration | **prepared**: pull request #1 open for review, not merged |
| Infrastructure validated and planned (mocked) | **pass** |
| Read-only discovery; plan against an account; apply | **blocked**: inputs and approval (STAGING_PLAN.md) |
| Image in a registry by digest; deployment; URL | **blocked** |
| RDS restricted administrator; verified TLS; hardened runtime; proxy trust; cookies; OIDC | pass locally; hosted **blocked** |
| SUID/SGID assertion; eight advisories | **pass**; triage owner not named, expires 2026-11-07 |
| Live model, real IdP, real feed, hosted telemetry, managed restore | **blocked** |
| Restricted pilot / production | **NO-GO** ([PILOT_DECISION.md](PILOT_DECISION.md)) |

## What is needed next

1. **Review pull request #1** once its checks pass; merging it is the owner's decision,
   and it makes `publish-staging` dispatchable.
2. **Supply the inputs** in [STAGING_PLAN.md](STAGING_PLAN.md) ("Inputs needed"),
   including an IAM Identity Center profile you have logged in to, then approve that
   plan and its spend. Read-only discovery and the real plan follow; nothing billable is
   created before approval.
3. **Bedrock**, separately: a model profile, permitted destinations, and a token and
   dollar cap for the smoke evaluation.
