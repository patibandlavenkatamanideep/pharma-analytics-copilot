# Restricted AWS staging — plan for approval

**For approval before anything billable is created.** Prepared 9 October 2026 for
candidate `fb415ca`. It reuses [infra/aws-staging](../infra/aws-staging/README.md); every
number below is either measured, taken from AWS's public price list, or marked as an
assumption. Nothing in it has run on AWS.

## What this stage is, and is not

**Infrastructure smoke staging**, with synthetic data and the offline planner: it shows
that the release image runs on AWS as designed: RDS with a restricted administrator,
certificate-verified TLS, the load balancer's path, the hardened Fargate runtime, sign-in
and sign-out over HTTPS, and the lifecycle (teardown and recreate). It is **not** a
restricted pilot: no live model (answers come from the offline planner, not live
NL2SQL), no real identity provider unless one is registered, no real feed, one task (not
highly available), a Single-AZ database (no failover), and no hosted telemetry. Each of
those is a separately costed extension below, enabled only when approved.

## Decided so far, and still open (9 October)

| | Decision |
|---|---|
| Account | a new member account, `pac-staging`, in a new AWS organization; the owner signs in through IAM Identity Center with read-only access (an expiring session; no access key) |
| Read-only discovery | done: the account is empty (only AWS's default VPC), no GitHub OIDC provider, no DNS zone, quotas sufficient (Fargate 6 vCPUs) |
| Region | `us-east-1`; no residency requirement stated |
| Budget and lifetime | **$25–30 in total**, for 1–2 weeks of review |
| Owner | Venkata Manideep |
| Alert email, testers' address | supplied; kept out of this public repository (they go only into the ignored variable file) |
| Domain | **none** |
| **Open: shape** | for a scheduled review week with the app available all day: the module's defaults (`db.t4g.small`, full dataset; $3.15/day, **$22.07 for 7 days**), or option A (`db.t4g.micro`, seed data; $2.76/day, $19.32 for 7 days); option B (app about 8 hours a day; $1.87/day, $13.09 for 7 days) only if reviewers keep known hours. None includes a domain, DNS hosting or model usage |
| **Open: address** | an existing domain if the owner has one; otherwise a cheap one bought in Route 53 with auto-renew off (check the registration and renewal prices before buying); or, with no domain, CloudFront's own HTTPS hostname, which needs design changes (it moves the IP restriction to CloudFront and changes which client address the application sees, so the sign-in lockout must be re-verified); or a self-signed certificate, with a browser warning for every visitor |

The September demo's `https://44-217-117-172.sslip.io` cannot be reused: that address
belonged to the deleted EC2 host, the load balancer has no fixed address, and AWS issues
certificates only for domains whose DNS the owner controls.

## For a review week

The owner's purpose is to show the work to reviewers for about a week, not to run it for
months. That adds four conditions:

* **Schedule it around actual reviewers**: every day it exists is billed whether anyone
  visits or not.
* **Reviewer access.** The module admits only `allowed_cidrs`, and refuses `0.0.0.0/0`.
  Reviewers' addresses must be collected and added, or the owner decides explicitly to open
  the site to everyone for the week. That needs a deliberate change to the module, which
  today refuses it; sign-in, the per-address lockout and the request limits still apply.
  A domain alone does not make the site reachable.
* **Live NL2SQL is separate.** These estimates use the offline planner. A model budget
  covers two things, each capped where it is spent: reviewers' questions, by the
  application's shared allowance (`llm_spend_limit_usd`: one limit for every user, worker
  and task, enforced before each model call, its repair included; RUNBOOK.md, "Model
  allowance"), and the evaluation run, by its own token caps. A $5 budget, for example:
  $3 for the website (about 100 questions at the measured ~$0.03 each, though each call
  first reserves its worst case, ~$0.25) and a smoke evaluation capped at $2. Both need
  the owner's own token and dollar budget; an AWS budget on Bedrock alerts as well.
* **Record a walkthrough before teardown**, so the evidence outlasts the deployment.

A week of the module's defaults ($22.07) plus a domain and a small model allowance can
exceed the $25–30 first stated; the owner confirms the total.

## Recommended shape

| Part | Choice | Why |
|---|---|---|
| Compute | 1 Fargate task, 1 vCPU / 2 GB, linux/amd64 | the image's two uvicorn workers; one task is enough for smoke tests |
| Egress | tasks in public subnets with a public address, inbound only from the load balancer (`egress_mode = "public_ip"`) | no NAT gateway ($34/month more); the security groups admit the tasks only from the load balancer (policy test), shown on AWS in stage 7 |
| Database | RDS PostgreSQL 16, Single-AZ, 20 GB gp3, 7-day backups: **`db.t4g.small` with the full 2,000,000-row synthetic dataset** (the module's default), or `db.t4g.micro` with the seed data ($0.38 a day less) | the default shows the full dataset at its real size; micro saves about $2.70 a week |
| Edge | internet-facing ALB, HTTPS only from the testers' address | `0.0.0.0/0` is refused |
| Model | offline planner (`llm_provider = "offline"`) | no spend; Bedrock needs its own budget |
| Telemetry | none hosted (`enable_observability = false`) | ~$22/month for one task; traces would still go only to a debug exporter |
| Lifetime | one scheduled review week, the app available all day; a walkthrough recorded; then torn down | every day the load balancer and database exist is billed |

## Cost

From [cost/ESTIMATE.md](../infra/aws-staging/cost/ESTIMATE.md) (us-east-1 list prices,
offers dated 2026-09-11 to 2026-10-07): option A **$2.76 a day** ($19.32 for 7 days,
$27.59 for 10); option B **$1.87 a day** ($26.18 for 14 days); the module's defaults
**$3.15 a day** ($44.14 for 14 days). What runs all the time: the load balancer
($0.54 a day), the database (micro $0.38, small $0.77), its storage and three public
IPv4 addresses; the serving task ($1.18 a day when always on) is what option B
saves. The price files are dated 2026-09-11 to 2026-10-07; before approval they are
refreshed from the then-current price list. Budgets send email; they do not stop spending. No free-tier credit is assumed.

| After teardown | Cost |
|---|---|
| Final RDS snapshot (~2 GB) | ~$0.19/month until deleted |
| Secrets in their 7-day recovery window | per Secrets Manager's terms; deleted after 7 days |
| Images kept in ECR (if the repository is kept) | $0.10 per GB-month |
| Terraform state bucket | cents |

| Extension (each separately approved) | Adds |
|---|---|
| Prometheus workspace with alerts to SNS email, one task | ~$22/month (two tasks ~$47) |
| Replica and failover test (2 tasks, Multi-AZ) | to ~$213/month while it runs (~$7/day) |
| Private tasks behind one NAT gateway | +$34/month |
| Bedrock live-model smoke evaluation | at most $2.75 at list rates (150k input / 70k output tokens) |
| Bedrock regression sets | about $2 |

## What is created

[PLAN_INVENTORY.md](../infra/aws-staging/PLAN_INVENTORY.md), from plans with a mocked
provider: **62 resources** for the foundation, **71** once an image is set (the serving
and seven one-shot task definitions and the service). The real plan's counts must match;
a difference is explained before apply.

**IAM.** Two execution roles (serving: only the three serving roles' secrets and the
OIDC client secret; one-shot: every database secret and the RDS-managed administrator
secret); two task roles with no permission (Bedrock and Prometheus only when enabled);
the flow-logs role; the GitHub OIDC provider (or an existing one) and a publish-only
role trusted for this repository's `staging` environment. No role can deploy except the
owner's own session.

**Security exceptions** (trivy config; owner: Venkata Manideep until delegated):
internet-facing load balancer, limited to `allowed_cidrs` (HIGH, accepted); RDS IAM
authentication off (MEDIUM, scram passwords from Secrets Manager); AWS-managed instead
of customer-managed KMS keys, no Container Insights, no Performance Insights (LOW, cost).

## Stages

| # | Stage | Approval | Verified by |
|---|---|---|---|
| 0 | Read-only discovery with your expiring session: caller identity, region, permissions, quotas, existing resources, an existing GitHub OIDC provider | your access | a redacted summary |
| 1 | State backend: an S3 bucket, versioned, encrypted, public access blocked, access limited to your role, `use_lockfile` | this plan | bucket settings read back |
| 2 | Foundation (`terraform plan`, reviewed, then apply); certificate DNS validation | this plan | plan summary; TLS on the URL |
| 3 | Secret values (`seed-secrets.sh`; nothing in Git, logs or Terraform) | this plan | "set" lines only |
| 4 | Publish the image: requires the release integration pull request ([#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1)) merged to `main`, by your review | PR approval | run summary: commit, tested image, registry digest |
| 5 | Protect the image (`keep-` tag; retained up to 9,999 such images, not forever); task definitions at 0 tasks | this plan | an ECR lifecycle preview lists no `keep-` image for expiry; the plan refuses an unprotected digest |
| 6 | `bootstrap` (restricted RDS administrator), `load-full` (the full synthetic dataset), `dataset-check`, `boundary`, `test-users` | this plan | each exit code; `dataset-check` prints the loaded counts and exits 1 unless 2,000,000 sales rows and 40,000 organizations; logs hold no value |
| 7 | One serving task: Fargate volume ownership, readiness, verified TLS, the load balancer's path; browser sign-in, an answer, sign-out on the HTTPS URL | this plan | records naming commit, digest, configuration |
| 8 | Lifecycle: teardown and recreate within the budget (secret generations, snapshot label, kept images) | this plan | the recreate succeeds |
| 9 | Record a walkthrough of the live system first; then teardown at the end of the review week; residual costs as above | this plan | nothing left but what the owner chooses to keep. Charges already incurred can appear on a later bill, also after an account is closed |

**Rollback and recovery limits.** No earlier release is a rollback target
(ROLLBACK_DECISION.md); a bad release is fixed forward. ECS rolls back a failed
deployment by itself. The database restores from an automated backup to a new instance
(point in time, 7 days); the first staging cycle measures it. One Single-AZ instance
means no failover is tested in this stage.

## Inputs

| Input | State |
|---|---|
| An IAM Identity Center profile on the owner's Mac | **done** (`pac-staging`, read-only) |
| Region and data residency | **done**: `us-east-1`; no requirement stated |
| Budget and lifetime | **done**: $25–30 in total, 1–2 weeks |
| Alert recipient and resource owner | **done** (the address is kept out of this repository) |
| Testers' networks | **done**: one address (kept out of this repository); a home address can change, and then the allowed list is updated |
| Cost option (A or B) | **open** |
| Hostname: an existing domain, one to buy, CloudFront's hostname (design changes), or a self-signed certificate | **open** |
| At approval: a second permission set, assigned to `pac-staging` only, able to create the approved resources | **open**; the read-only session cannot create anything |
| A final-snapshot label (a date is enough) | at teardown |
| Later, each its own approval: identity-provider registration and role mapping; feed samples and source contract; Bedrock model, destinations and token/dollar caps; service, freshness, RTO/RPO, audit-mode and retention targets | open |
