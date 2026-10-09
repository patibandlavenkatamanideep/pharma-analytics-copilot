# Restricted AWS staging — plan for approval

**For approval before anything billable is created.** Prepared 9 October 2026 for
candidate `fb415ca`; updated the same day with the owner's decisions. It reuses [infra/aws-staging](../infra/aws-staging/README.md); every
number below is either measured, taken from AWS's public price list, or marked as an
assumption. Nothing in it has run on AWS.

## What this stage is, and is not

**A review week on AWS**, with synthetic data: it shows that the release image runs on
AWS as designed (RDS with a restricted administrator, certificate-verified TLS, the load
balancer's path, the hardened Fargate runtime, sign-in and sign-out over HTTPS), first
with the offline planner, then with a live model inside a small allowance for the
reviewers the owner approves. It is **not** a restricted pilot: no real identity
provider unless one is registered, no real feed, one task (not highly available), a
Single-AZ database (no failover), and no hosted telemetry. Each of those is a separately
costed extension below, enabled only when approved.

## The owner's decisions (9 October)

| | Decision |
|---|---|
| Account | a new member account, `pac-staging`, in a new AWS organization; the owner signs in through IAM Identity Center (expiring sessions, read-only or a one-hour administrator set; no access key) |
| Read-only discovery | done: the account is empty (only AWS's default VPC), no GitHub OIDC provider, quotas sufficient (Fargate 6 vCPUs) |
| Region | `us-east-1`; no residency requirement stated |
| Dates | **seven days from 9 October**: live as soon as this plan is approved and deployed, torn down by **16 October 2026** |
| Shape | the module's defaults: `db.t4g.small`, the full synthetic dataset, the app available all day ($3.15 a day, **$22.07 for 7 days**) |
| Access | *"Anyone may reach the sign-in page, but only accounts I approve may use the application."* `public_sign_in = true` opens the load balancer's 443 and 80 to everyone, nothing else; each reviewer gets an account of their own from the owner's list (`seed-secrets.sh --reviewers`, the `reviewers` task); there is no sign-up |
| Address | `pharma-copilot.click`, registered by the owner in Route 53 in `pac-staging` ($3 a year; renewal $3, auto-renew to be turned off); the site at `https://staging.pharma-copilot.click`. Its registration and hosted zone are verified read-only before the plan |
| Live model | **$5**: $3 for the website (`llm_spend_limit_usd = 3`, the shared allowance) and $2 for the evaluation (caps of 200,000 input and 30,000 output tokens, $1.93 at list prices; reruns counted) |
| Total budget | **$40**, confirmed; `monthly_budget_usd = 40`. Budgets send email; they do not stop spending |
| Owner | Venkata Manideep |
| Alert email, tester's address | supplied; kept out of this public repository (they go only into the ignored variable file) |

The September demo's `https://44-217-117-172.sslip.io` cannot be reused: that address
belonged to the deleted EC2 host, the load balancer has no fixed address, and AWS issues
certificates only for domains whose DNS the owner controls.

## For a review week

The owner's purpose is to show the work to reviewers for about a week, not to run it for
months. That adds four conditions:

* **Schedule it around actual reviewers**: every day it exists is billed whether anyone
  visits or not.
* **Reviewer access.** By default the module admits only `allowed_cidrs`, and refuses
  `0.0.0.0/0` there. The owner decided to open the sign-in page to everyone for the week:
  `public_sign_in` adds two rules, 443 and 80 to the load balancer from anywhere, and
  nothing else (the tasks and the database stay reachable only from it). Sign-in, the
  per-address and per-account lockout and the request limits still apply, and only the
  accounts on the owner's list exist.
* **Live NL2SQL is separate.** These estimates use the offline planner. A model budget
  covers two things, each capped where it is spent: reviewers' questions, by the
  application's shared allowance (`llm_spend_limit_usd`: one limit for every user, worker
  and task, enforced before each model call, its repair included; RUNBOOK.md, "Model
  allowance"), and the evaluation run, by its own token caps. A $5 budget, for example:
  $3 for the website (about 100 questions at the measured ~$0.03 each, though each call
  first reserves its worst case, ~$0.25) and $2 for evaluation: caps of 200,000 input and
  30,000 output tokens are $1.93 at the `us.` profile's list prices (above the smoke run's
  expected usage plus one call's reservation). The $2 counts every run: a rerun gets only
  what remains, its caps computed from the earlier runs' recorded token counts. Both are
  calculated from provider-reported usage and configured prices, not read from the
  invoice; an AWS budget on Bedrock alerts as well.
* **Record a walkthrough before teardown**, so the evidence outlasts the deployment.

A week of the module's defaults ($22.07), the domain ($3), its hosted zone ($0.50 a
month) and the model allowance ($5) come to about $31 of the $40 confirmed, before
refreshed prices; the remainder covers DNS queries, the residual costs below and
estimate error. No free-tier credit is assumed.

## Recommended shape

| Part | Choice | Why |
|---|---|---|
| Compute | 1 Fargate task, 1 vCPU / 2 GB, linux/amd64 | the image's two uvicorn workers; one task is enough for smoke tests |
| Egress | tasks in public subnets with a public address, inbound only from the load balancer (`egress_mode = "public_ip"`) | no NAT gateway ($34/month more); the security groups admit the tasks only from the load balancer (policy test), shown on AWS in stage 7 |
| Database | RDS PostgreSQL 16, Single-AZ, 20 GB gp3, 7-day backups: **`db.t4g.small` with the full 2,000,000-row synthetic dataset** (the module's default), or `db.t4g.micro` with the seed data ($0.38 a day less) | the default shows the full dataset at its real size; micro saves about $2.70 a week |
| Edge | internet-facing ALB, HTTPS (HTTP redirects); open to everyone for the sign-in page (`public_sign_in`) | the owner's access decision; only approved accounts can sign in |
| Model | offline planner for the smoke stages; then Bedrock (`us.` Claude profile) with the $3 website allowance | spend only within the approved $5 |
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
provider: **63 resources** for the foundation, **73** once an image is set (the serving
and eight one-shot task definitions and the service), and 2 more with the public sign-in
page. The real plan's counts must match; a difference is explained before apply.

**IAM.** Two execution roles (serving: only the three serving roles' secrets and the
OIDC client secret; one-shot: every database secret and the RDS-managed administrator
secret); two task roles with no permission (Bedrock and Prometheus only when enabled);
the flow-logs role; the GitHub OIDC provider (or an existing one) and a publish-only
role trusted for this repository's `staging` environment. No role can deploy except the
owner's own session.

**Security exceptions** (trivy config; owner: Venkata Manideep until delegated):
internet-facing load balancer, open to everyone on 443 and 80 by the owner's decision for
the review week, or limited to `allowed_cidrs` without it (HIGH, accepted); RDS IAM
authentication off (MEDIUM, scram passwords from Secrets Manager); AWS-managed instead
of customer-managed KMS keys, no Container Insights, no Performance Insights (LOW, cost).

## Stages

| # | Stage | Approval | Verified by |
|---|---|---|---|
| 0 | Read-only discovery with your expiring session: caller identity, region, permissions, quotas, existing resources, an existing GitHub OIDC provider | your access | a redacted summary |
| 1 | State backend: an S3 bucket, versioned, encrypted, public access blocked, access limited to your role, `use_lockfile` | this plan | bucket settings read back |
| 2 | Foundation (`terraform plan`, reviewed, then apply); certificate DNS validation | this plan | plan summary; TLS on the URL |
| 3 | Secret values (`seed-secrets.sh`, with `--reviewers` for the owner's list; nothing in Git, logs or Terraform) | this plan | "set" lines and counts only |
| 4 | Publish the image: requires the release integration pull request ([#1](https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot/pull/1)) merged to `main`, by your review | PR approval | run summary: commit, tested image, registry digest |
| 5 | Protect the image (`keep-` tag; retained up to 9,999 such images, not forever); task definitions at 0 tasks | this plan | an ECR lifecycle preview lists no `keep-` image for expiry; the plan refuses an unprotected digest |
| 6 | `bootstrap` (restricted RDS administrator), `load-full` (the full synthetic dataset), `dataset-check`, `boundary`, `test-users`, `reviewers` | this plan | each exit code; `dataset-check` prints the loaded counts and exits 1 unless 2,000,000 sales rows and 40,000 organizations; `reviewers` prints counts; logs hold no value |
| 7 | One serving task, offline planner: Fargate volume ownership, readiness, verified TLS, the load balancer's path; browser sign-in, an answer, sign-out on the HTTPS URL; from an address outside `allowed_cidrs`, the sign-in page loads and an unknown account is refused | this plan | records naming commit, digest, configuration |
| 7a | Live model: Bedrock model access granted in `pac-staging` (the owner's form); `llm_provider = "bedrock"`, `enable_bedrock`, the $3 allowance and list prices; one live answer, then the evaluation within its $2 caps | this plan, within the $5 | the allowance row and the evaluation's recorded token counts; AWS billing as the truth |
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
| An IAM Identity Center profile on the owner's Mac | **done** (`pac-staging`: read-only, and a one-hour administrator set assigned to `pac-staging` only) |
| Region and data residency | **done**: `us-east-1`; no requirement stated |
| Dates and total budget | **done**: seven days, torn down by 16 October 2026; $40 in total |
| Alert recipient and resource owner | **done** (the address is kept out of this repository) |
| Testers' networks and access | **done**: the sign-in page open to everyone; one tester address also in `allowed_cidrs` (kept out of this repository) |
| Shape | **done**: the module's defaults (`db.t4g.small`, full dataset, always on) |
| Hostname | **done**: `staging.pharma-copilot.click`; registration to be confirmed read-only |
| Live model | **done**: $5 ($3 website, $2 evaluation with token caps); Bedrock model access is requested by the owner in `pac-staging` |
| Reviewers' list (email, name, the user whose scope each gets) | **open**: a CSV the owner keeps outside the repository |
| A final-snapshot label (a date is enough) | at teardown |
| Later, each its own approval: identity-provider registration and role mapping; feed samples and source contract; service, freshness, RTO/RPO, audit-mode and retention targets | open |
