# AWS staging — status

**Status as of 9 October 2026.** Staging is **deployed** in the `pac-staging` account
(us-east-1) under the plan and spend the owner approved ([STAGING_PLAN.md](STAGING_PLAN.md)):
**https://staging.pharma-copilot.click**, until 16 October. It serves release `5536526` from the registry
by digest (`sha256:bf04b0175a274fb3d7b62b13edc0c9c51098379d56f3d7e675e5042d9f90057c`), with the infrastructure from `a786bda`. Each
result below says where it ran; "hosted" means on that account. Neither a restricted
pilot nor production readiness can be claimed ([PILOT_DECISION.md](PILOT_DECISION.md)).

## Head, CI and branch protection

| Item | State | Identity |
|---|---|---|
| Branch on GitHub | `the branch head, after the candidate: evidence and documentation only` (pushed normally; never forced) | `codex/release-defects-oct06` |
| Executable candidate | `a786bda` | full local release chain: `r5-rc-a786bda-*.json` |
| Hosted CI on the candidate | all four jobs passed | run [37957713810](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37957713810) |
| `main` | `6b139a7` (pull request #1, merged by the owner on 9 October with a merge commit; its tree is `5536526`'s), protected (read back 9 October): pull request required; checks `test`, `frontend`, `supply-chain`, `image` bound to GitHub Actions (app 15368) and up to date; administrators included; no force-push or deletion; conversations resolved; 0 approvals (one maintainer) | `evidence/external.json` |
| `staging` environment | required reviewer the owner; administrators cannot bypass; deployments from `main` and `codex/release-*` only | `evidence/external.json` |
| Registered workflows | `CI` and `publish-staging` (on `main` since #1) | [#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1), merged |

Hosted CI on the way to the candidate — each failure was reproduced and fixed, none
retried away:

| Run | Commit | Result | What it showed |
|---|---|---|---|
| 37865432807 | `e6cb251` | failure | `test`: the ledger check needs the whole history; CI checked out one commit |
| 37866339109 | `811713c` | failure | `supply-chain`: the provider lock held a package hash for the Mac only |
| 37866607406 | `f874b89` | failure | `supply-chain` as above; `test` passed with the whole history |
| 37866723060 | `aea40b2` | failure | `test`: a browser journey found the previous user signed in after Sign out |
| 37868427618 | `8157017` | success | first sign-out fix |
| 37869897240 | `e101db3` | success | the previous candidate: all four jobs, the Terraform step included |
| 37920515182 | `fb415ca` | success | the website's model allowance, the dataset check |
| 37933355116 | `d004a20` | success | reviewers' own accounts, the public sign-in page; its module was applied |
| 37936348151 | `5536526` | success | the plan after the apply is empty; the published image's commit |
| 37939037102 | `5536526` | publish: attempt 1 failure, attempt 2 success | attempt 1: the publish role refused GitHub's token (it trusted the legacy subject; reproduced, fixed in `facd0d4`, applied); attempt 2 pushed `sha256:bf04b0175a27…` |
| 37940273033 | `facd0d4` | success | the publish role trusts the subject GitHub sends |
| 37957713810 | `a786bda` | success | the candidate: the load balancer adds browser security headers |

**History.** On 8 October this document said GitHub's copy ended at `69c62de`, the
last hosted run was 37653591687 and `main` had no protection; all three were true then.

## Infrastructure as code

Terraform, the tool the repository already used (`infra/terraform`, the earlier EC2
demo), in a separate module, [infra/aws-staging](../infra/aws-staging/README.md):
inventory, owner inputs, state, create/update/rollback/failover/teardown order and
cleanup are in its README; the plan for approval is [STAGING_PLAN.md](STAGING_PLAN.md).

| Check | Result | Where | Record |
|---|---|---|---|
| Lock read-only, `fmt`, `validate`, mocked plans | passed | CI (`supply-chain`) on `a786bda` | run 37957713810 |
| 14 mocked plans: the foundation (63 resources), deployable (73), the public sign-in page (+2), NAT, failover; refusals of a world-open CIDR, a tag for a digest, SSO without a client id, an unpinned collector, an image without a `keep-` tag, a live model without an allowance, a zero price and a wildcard GitHub subject | passed | local | `r5-staging-terraform-test-a786bda.json` |
| Every address each configuration would create | generated | local | `infra/aws-staging/PLAN_INVENTORY.md` |
| The real plan, read-only, with the owner's variables | 68 to add, 0 to change, 0 to destroy; every address reconciled with the inventory (`evidence/probes/staging_plan_check.py`) | hosted (plan only) | the probe's count line |
| Apply: foundation; then image, task definitions, service | 68 added; 10 added; the service at 1 task | hosted, from exact archives of the module | `r5-staging-plan-drift-*.json` |
| Plan straight after the apply | **was not empty** (two normalisations AWS makes); fixed in `5536526`, then 0/0/0 after every later change | hosted | `r5-staging-plan-drift-reproduced.json` → `-fixed.json` |
| `trivy config` 0.58.1, with the public sign-in page | only accepted findings (1 HIGH: internet-facing load balancer, open to everyone on 443 and 80 by the owner's decision; 1 MEDIUM: IAM database authentication off; LOW: AWS-managed keys, no Container Insights or Performance Insights) | local, `a786bda` | `r5-staging-trivy-config-a786bda.json` |
| Security properties from source | passed | CI and local | `r5-rc-a786bda-pytest.json` |

Lifecycle, designed before the first teardown and verified only on AWS (STAGING_PLAN.md,
stage 8): ECR retains `keep-*` images (up to 9,999 of them; a lifecycle preview confirms
the rule order once the repository exists) and a deploy refuses an unprotected digest;
secret names carry a generation, so a recreate inside the 7-day recovery window restores
and imports or moves to the next generation; the final snapshot is named by an
operator-chosen label.

## Cost

[infra/aws-staging/cost/ESTIMATE.md](../infra/aws-staging/cost/ESTIMATE.md), from AWS's
public price list for us-east-1 (13 offers dated 2026-09-11 to 2026-10-07): about **$96
a month ($3.15 a day)** for low-cost staging (one task, Single-AZ, offline planner, no
hosted telemetry) and about **$213** while replica and failover tests run. Its prices were re-checked on 9
October: 11 of 13 offers unchanged, Bedrock's offer re-published with the same rates, and
the EC2 offer (the NAT gateway, not used) changed. Model calls cost about $30 per 1,000
questions at the `us.` profile's list rates. Budgets alert; they do not cap.

## Image, publishing, deployment identity, URL

| Item | State |
|---|---|
| Image | published by `publish-staging` (run 37939037102, attempt 2) from `5536526`: built for linux/amd64 without a cache, scanned, its hardened journeys run, then pushed; the registry's manifest checked against the tested image |
| Registry digest | `sha256:bf04b0175a274fb3d7b62b13edc0c9c51098379d56f3d7e675e5042d9f90057c`, tags `5536526…` and `keep-20261009-5536526` |
| Retention | the lifecycle preview, with the `keep-` tag on, lists no image for expiry |
| Publishing identity | role `pac-staging-github-publish`, trusted only for this repository's `staging` environment in the subject format GitHub sends; pushes images and nothing else |
| Deploying identity | the owner's IAM Identity Center session (administrator set, one hour), applying reviewed plans from exact archives of the module; state in a versioned, encrypted S3 bucket usable only by the account's two Identity Center roles |
| URL | **https://staging.pharma-copilot.click** (certificate issued by Amazon for this host; HTTP redirects to HTTPS) |
| Approved reviewers | one: the owner (`seed-secrets.sh --reviewers`, the `reviewers` task) |

## Integrations

Local results are named as local; "hosted" is filled only by a run on AWS.

| Requirement | Local evidence | Result | Hosted |
|---|---|---|---|
| RDS with a restricted administrator | as a role with only CREATEROLE and CREATEDB: failed before the fix (`739957a`), passed after (`4ab8fd7`): provisioning, boundary, an answer, restore into a new cluster | pass (emulated) | **pass**: `bootstrap` as the RDS master user, then `boundary` ("boundary intact"), both exit 0 |
| No owner, superuser or BYPASSRLS for the serving application | the boundary check after provisioning and restore; the serving task gets only the serving roles' secrets | pass | **pass**: `boundary` on RDS; the live site answers within each user's scope |
| Certificate-verified TLS | a TLS-only cluster with its own CA; another CA and plaintext refused (`4a234e2`); the image pins the RDS bundle (checked by its smoke test) | pass (private CA) | **pass** for `verify-full`: every task connected with the pinned bundle (exit 0); `rds.force_ssl` is 1 and in sync. The negative run (another `PGSSLROOTCERT`) **not yet run** |
| Non-root, capabilities dropped, read-only root | image smoke at `fb415ca`, 28 checks: read-only root without tmpfs, every capability dropped, no-new-privileges, one writable volume, no setuid or setgid file | pass | **pass**: the task definition reads `10001`, `true`, `["ALL"]`; `load-full` wrote its one volume (exit 0) |
| Proxy trust | untrusted, 20 failures from other clients refuse a user's password (429); trusted, accepted (`128c558`); a forged `X-Forwarded-For` cannot choose the client (guard test) | pass (local proxy) | **not yet run**: needs a second network |
| Cookies and sign-out | Secure, HttpOnly, SameSite=Lax over HTTPS; sign-out shows the sign-in form only once the server has ended the session, never presents an unconfirmed sign-out as done, and leaves no figure, question or conversation id of the first user (15 journeys, `7a1a2bb`) | pass (local) | **pass** for cookies: `Secure; HttpOnly; SameSite=lax; Path=/` at sign-in and sign-out, and sign-out ends the session (live check). The 15 journeys were not run against the site |
| OIDC | the security suite against an in-process provider | pass (local) | blocked: no registration with a real provider |
| Model (Bedrock) | offline planner only; the website's shared allowance (one limit for every user, worker and task, enforced before each call) passes its tests with a fake model transport (`r5-website-allowance-fixed.json`) | allowance: pass (local) | **blocked**: $5 approved; the account's Anthropic use-case form not yet submitted |
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

The setuid/setgid assertion is kept in the image smoke and CI's image job. The images built
from `5536526` (published) and `a786bda` have none. Its unfiltered scan (trivy 0.58.1, database of 2026-10-08 19:05Z)
matches `e551650`'s exactly: 164 findings, the same 8 HIGH advisories, none fixable in
Debian, none in Python packages, each reviewed against the deployed configuration in
[VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md).

## Live checks

`evidence/probes/staging_live_check.py`, over HTTPS from outside AWS (the tester's
network): HTTP redirects to HTTPS; `/health` names `5536526`; `/ready`; an unknown account
and a cross-site sign-in refused; U001 (executive) sees the full dataset (2,000,000 sales
rows), gets a revenue answer, and is signed out after signing out; U003 (a regional
director without pricing) asking for revenue gets volume with the restriction stated and
no currency or price column (the evaluation's own rule, which fails on the executive's
answer), and a scoped volume answer. **13 of 13 pass** (`r5-staging-headers-fixed.json`),
including the browser security headers the load balancer now adds: the first run found
none of them (`r5-staging-headers-reproduced.json`).

A walkthrough of the live site, as eight screenshots taken by Chromium through the public
hostname, is kept in [`evidence/walkthrough/2026-10-09/`](../evidence/walkthrough/2026-10-09/):
the sign-in page; the executive's top accounts, revenue ($250,766,926.42 for the rolling three
months) and a brand's performance; the Northeast director asking for revenue and getting
68,236 packs with the restriction stated; the same director's territories (New York Metro and
New England, which sum to that total); and sign-out for both.

## Pass / fail / blocked

| Gate | Status |
|---|---|
| Local release chain on the candidate | **pass** |
| Hosted CI on the candidate | **pass** (run 37957713810) |
| Branch protection and the `staging` environment | **pass**; the environment's reviewer approved both publishing attempts |
| Release integration | **pass**: #1 merged by the owner |
| Infrastructure validated and planned (mocked); real plan; apply | **pass**; the plan after the first apply was not empty (fixed in `5536526`), and after every apply since it is |
| Image in a registry by digest; deployment; URL | **pass** |
| RDS restricted administrator; verified TLS; hardened runtime; cookies | **pass** on AWS (TLS negative run outstanding) |
| Proxy trust from two networks; the sign-in page from outside `allowed_cidrs` | **not yet run** |
| SUID/SGID assertion; eight advisories | **pass**; triage owner not named, expires 2026-11-07 |
| Live model, real IdP, real feed, hosted telemetry, managed restore, failover | **blocked** |
| Restricted pilot / production | **NO-GO** ([PILOT_DECISION.md](PILOT_DECISION.md)) |

## What is needed next

1. **From outside the allowed address** (a phone on mobile data): the sign-in page
   loads; with a second network, the proxy-trust check above.
2. **Reviewers**: their names, addresses and scope, for `seed-secrets.sh --reviewers`.
3. **Live model**: the account's Anthropic use-case form; then `llm_provider =
   "bedrock"` with the $3 website allowance, and the evaluation within its $2 caps.
4. **A walkthrough** of the live site before teardown; **teardown by 16 October**
   (`deletion_protection = false`, a final-snapshot label, then destroy; the hosted
   zone, kept snapshot and images as decided).
