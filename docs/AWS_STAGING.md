# AWS staging — status

**Date:** 8 October 2026. **Candidate:** `e551650` on `codex/release-defects-oct06`.
**Summary:** staging is prepared and locally verified, and **not deployed**. Every
step that needs AWS or a change to GitHub is blocked on authorization this work
did not have. Nothing below was run on AWS, and no emulation is reported as a
hosted result. Neither pilot nor production readiness can be claimed.

## Head, CI and branch protection

| Item | State | Identity |
|---|---|---|
| Local candidate | `e551650`, clean tree, 84 commits after GitHub's copy up to it (then evidence and documentation) | full release chain: `r5-rc-e551650-*.json` |
| GitHub's copy of the branch | `69c62de` | push from this session denied by its permission check; not attempted any other way |
| Hosted CI | **`69c62de` only**: run [37653591687](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/actions/runs/37653591687), all four jobs passed | nothing after `69c62de` has hosted CI |
| Branch protection on `main` (`c8aab5b`) | **none**: no protection, no rulesets, no environments (read-only API, 8 October) | changing it was not authorized |

## Infrastructure as code

Terraform, the infrastructure-as-code tool the repository already used (`infra/terraform`,
the earlier EC2 demo), in a separate module, [infra/aws-staging](../infra/aws-staging/README.md):
inventory, owner inputs, state, create/update/rollback/failover/teardown order and
cleanup are in its README.

| Check | Result | Record |
|---|---|---|
| `terraform validate` (1.16.5, AWS provider 6.68.0, lock committed) | passed | `r5-staging-terraform-validate.json` (`4c90704`) |
| `terraform fmt -check` | passed | `r5-staging-terraform-fmt.json` |
| `trivy config` 0.58.1 | only accepted findings: 1 HIGH (internet-facing load balancer, restricted to `allowed_cidrs`), 1 MEDIUM (IAM database authentication off), 13 LOW (AWS-managed instead of customer-managed keys, Container Insights, Performance Insights) | `r5-staging-trivy-config.json`; report `r5-scan-staging-trivy-config.json` |
| Security properties from source (13 tests; 10 of 10 mutations caught) | passed | `r5-staging-policy-tests.json` |
| Workflows (`actionlint` 1.7.12) | passed | `r5-staging-actionlint.json` |
| **`terraform plan`** | **blocked**: needs credentials for the target account | — |
| **`terraform apply`** | **blocked**: provisioning not authorized | — |

`infra/aws-staging`, `publish-staging.yml`, the series probe and the policy tests are unchanged between `4c90704` and `e551650`.

## Cost

[infra/aws-staging/cost/ESTIMATE.md](../infra/aws-staging/cost/ESTIMATE.md), generated
from AWS's public price list for us-east-1 (13 offers dated 2026-09-11 to
2026-10-07, each with its SHA-256; no account or API involved):

| Shape | USD a month | a day |
|---|---:|---:|
| Low-cost staging: 1 task, Single-AZ, no Prometheus, no model calls | 95.91 | 3.15 |
| Replica and failover tests: 2 tasks, Multi-AZ, Prometheus | 212.74 | 6.99 |
| + private tasks behind one NAT gateway | +33.75 | |
| + interface endpoints instead (not in the module) | +58.60 | |
| Model calls, `us.` Opus 4.5 profile | 30.09 per 1,000 questions | |

Prometheus is priced from a measured 588 series per process
(`r5-metric-series.json`), planned at 800. Model usage is priced from measured
tokens per question. An estimate, not a bill: nothing has run.

## Image, deployment identity, URL

| Item | State |
|---|---|
| Image | built locally from `e551650` for linux/amd64: config `219ddd92d0ac…`, local manifest `sha256:d266c32ae92e…` (podman, under Rosetta) |
| Registry digest | **none**: no registry push was authorized. The deployable digest is the one `publish-staging` reports; the laptop's image is not that image |
| Publishing identity | prepared: role `pac-staging-github-publish`, assumed only by this repository's jobs in environment `staging`; pushes images and nothing else. Not created |
| Deploying identity | the owner's IAM Identity Center session running a reviewed Terraform plan. Not exercised |
| URL | **none** |

## Integrations

Local results are named as local. "Hosted" is filled only by a run on AWS.

| Requirement | Local evidence | Result | Hosted |
|---|---|---|---|
| RDS compatibility with a restricted administrator | `restricted_admin_provisioning.py` as a role with only CREATEROLE and CREATEDB on PostgreSQL 16: failed before the fix (`r5-rds-admin-reproduced.json`, `739957a`), passed after (`r5-rds-admin-fixed.json`, `4ab8fd7`): provisioning, boundary, answer, restore into a new cluster | **pass (emulated)** | blocked |
| No owner, superuser or BYPASSRLS for the serving application | the boundary check after provisioning and after restore; the serving task gets only the three serving roles' secrets (policy test) | **pass** | blocked |
| Certificate-verified TLS | `db_tls_verify_full.py`: a TLS-only cluster with its own CA; bootstrap, load and an answer with `verify-full`; every application connection encrypted; another CA and plaintext refused (`r5-db-tls-verify-full.json`, `4a234e2`). The image pins the RDS global bundle (SHA-256 `fe45bbeb…`, checked by the image smoke) | **pass (private CA)** | blocked |
| Non-root, capabilities dropped, read-only root | image smoke at `e551650`: every container run read-only with no tmpfs, `--cap-drop ALL`, no-new-privileges; only `/app/schema/generated` writable; no setuid or setgid file; sign-in journeys on a fresh database | **pass**: 28 checks (`r5-rc-e551650-image.json`) | blocked (Fargate volume ownership unverified) |
| Proxy trust | `client_address_behind_proxy.py`: untrusted proxy, one client's 20 failures refuse everyone (429); trusted, not (`r5-proxy-client-address.json`, `b4315bb`). Staging trusts the VPC range only; the load balancer appends | **pass (local proxy)**; defect `proxy-client-address` | blocked |
| Cookies | over HTTPS with Secure on, as staging runs: sign-in sets `pac_session` Secure, HttpOnly, SameSite=Lax, Path=/, host-only; the OIDC binding cookie is `__Host-`. Nothing asserted the session cookie before: sign-out deleted it without Secure or HttpOnly — reproduced (`r5-logout-cookie-reproduced.json`, `11dd083`), fixed (`r5-logout-cookie-fixed.json`, `e551650`), defect `logout-cookie-attributes` | **pass (local)** | blocked |
| OIDC | the security suite's sign-in flows against an in-process test provider (`tests/security/fake_idp.py`) | **pass (local)**: security suite 436, strict (`r5-rc-e551650-security.json`) | blocked: no registration with a real identity provider |
| Model (Bedrock) | offline planner only | not run | blocked: paid inference not authorized |
| Metrics and alerts | local collector and Prometheus (earlier steps); series count measured | pass (local) | blocked: no Prometheus workspace |

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

The setuid/setgid assertion is kept in the image smoke and in CI's image job.
The image built from `e551650` has none (28 smoke checks). Its unfiltered scan (trivy 0.58.1, database of 2026-10-08 19:05Z) finds the same 8 HIGH advisories as before, 44 package findings, none fixable in Debian, none in Python packages. Each is reviewed against the deployed configuration in
[VULNERABILITY_TRIAGE.md](VULNERABILITY_TRIAGE.md).

## Pass / fail / blocked

| Gate | Status |
|---|---|
| Local release chain on the exact candidate | **pass**: boundary; security 436 and ingestion 177 strict; whole suite 2238 strict; 845 without a database; gate self-test 19; offline evaluations 38/38, 12/12, 12/12 (not model accuracy); 32 components; web build; 10 browser journeys; pip-audit, npm audit, gitleaks clean; image 28 checks; restore drill and upgrade compatibility on the same commit |
| Candidate pushed; hosted CI on it | **blocked**: push denied |
| Branch protection and a `staging` environment | **fail** (absent); changing them **blocked** |
| Infrastructure validated | **pass** |
| Infrastructure planned / applied | **blocked** |
| Inventory, cleanup and rollback procedure | **pass** (written; unexercised) |
| Region-specific estimate from current official prices | **pass** (estimate) |
| Image in a registry by digest | **blocked** |
| Deployment identity | prepared; **blocked** |
| Staging URL | **blocked** |
| RDS restricted administrator | pass (emulated); hosted **blocked** |
| Verified TLS | pass (local CA); hosted **blocked** |
| Hardened runtime | **pass** (local container runtime); Fargate **blocked** |
| Proxy trust, cookies, OIDC | pass (local); hosted and real IdP **blocked** |
| SUID/SGID assertion; eight advisories reviewed | **pass**: assertion kept; all eight unreachable in the deployed configuration; triage owner **not named**, expiry 2026-11-07 |
| Live model evaluation | **blocked** |
| Pilot | **not ready**: hosted CI with enforced protection, staging, a live evaluation, a real IdP and feed, and the owners' targets are all outstanding |

## Blocked actions, with the prepared command

Each needs the owner, or the owner's explicit authorization.

1. **Push** the candidate (the earlier push from this session was denied):
   `git push github codex/release-defects-oct06`, then follow
   [EXTERNAL_VERIFICATION_PLAN.md](EXTERNAL_VERIFICATION_PLAN.md) step 1 for hosted CI.
2. **Protect `main`** (required checks are CI's job names; 0 approvals because the
   repository has one maintainer — raise to 1 with a second):

   ```sh
   gh api -X PUT repos/patibandlavenkatamanideep/pharma-analytics-copilot/branches/main/protection \
     --input - <<'JSON'
   {"required_status_checks": {"strict": true, "checks": [
       {"context": "test"}, {"context": "frontend"}, {"context": "supply-chain"}, {"context": "image"}]},
    "enforce_admins": true,
    "required_pull_request_reviews": {"required_approving_review_count": 0, "dismiss_stale_reviews": true},
    "restrictions": null, "allow_force_pushes": false, "allow_deletions": false,
    "required_conversation_resolution": true}
   JSON
   ```

3. **Create the `staging` environment** with the owner as required reviewer, for
   `main` and release branches only:

   ```sh
   R=patibandlavenkatamanideep/pharma-analytics-copilot
   gh api -X PUT repos/$R/environments/staging --input - <<'JSON'
   {"reviewers": [{"type": "User", "id": 145270727}],
    "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
   JSON
   gh api -X POST repos/$R/environments/staging/deployment-branch-policies -f name=main
   gh api -X POST repos/$R/environments/staging/deployment-branch-policies -f name='codex/release-*'
   ```

4. **Plan, then apply, the infrastructure**: [infra/aws-staging/README.md](../infra/aws-staging/README.md), "Create", steps 1–3, with an IAM Identity Center session.
5. **Seed secrets, publish, deploy**: the same, steps 4–8.
6. **Live model evaluation** within a Bedrock budget: EXTERNAL_VERIFICATION_PLAN.md's
   smoke command (caps 150,000 input / 70,000 output tokens; at most $2.75 at the
   `us.` profile's list rates).

## What the owner decides

| Decision | Used by |
|---|---|
| AWS account, region and data residency (the `us.` model profile keeps inference in the US; `global.` does not) | `region`, `bedrock_model_id` |
| Monthly budget and who receives alerts | `monthly_budget_usd`, `alert_emails` |
| Model spend, if any | `model_budget_usd`, `llm_provider`, contracted rates |
| Staging hostname and DNS zone | `hostname`, `route53_zone_id` |
| Testers' networks | `allowed_cidrs` |
| Identity provider registration | `oidc_issuer`, `oidc_client_id` |
| Feed samples for ingestion | the ingestion onboarding (`evidence/readiness`) |
| A named release owner, who signs the vulnerability triage before 2026-11-07 | `VULNERABILITY_TRIAGE.md` |
| Whether Prometheus is worth ~$47 a month for two tasks, or the export interval goes from 15 s to 60 s | `enable_observability` |
