# Restricted AWS staging — plan for approval

**For approval before anything billable is created.** Prepared 9 October 2026 for
candidate `e101db3`. It reuses [infra/aws-staging](../infra/aws-staging/README.md); every
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

## Recommended shape

| Part | Choice | Why |
|---|---|---|
| Compute | 1 Fargate task, 1 vCPU / 2 GB, linux/amd64 | the image's two uvicorn workers; one task is enough for smoke tests |
| Egress | tasks in public subnets with a public address, inbound only from the load balancer (`egress_mode = "public_ip"`) | no NAT gateway ($34/month more); the security groups admit the tasks only from the load balancer (policy test), shown on AWS in stage 7 |
| Database | RDS PostgreSQL 16, `db.t4g.small`, Single-AZ, 20 GB gp3, 7-day backups | `db.t4g.micro` saves $11.68/month but has 1 GB of memory for a 1.2 GB dataset |
| Edge | internet-facing ALB, HTTPS only from the testers' networks | `0.0.0.0/0` is refused |
| Model | offline planner (`llm_provider = "offline"`) | no spend; Bedrock needs its own budget |
| Telemetry | none hosted (`enable_observability = false`) | ~$22/month for one task; traces would still go only to a debug exporter |
| Lifetime | **an owner decision**; the plan assumes 14 days | |

## Cost

From [cost/ESTIMATE.md](../infra/aws-staging/cost/ESTIMATE.md) (us-east-1 list prices,
offers dated 2026-09-11 to 2026-10-07): **$95.91 a month, $3.15 a day**, so about **$44**
for 14 days. Largest items: the task ($36.04/month), the database ($23.36), the load
balancer ($16.43 plus capacity units) and public IPv4 addresses ($10.95). Before
approval the estimate is refreshed for the chosen region from the then-current price
list. Budgets send email; they do not stop spending. No free-tier credit is assumed.

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
provider: **62 resources** for the foundation, **70** once an image is set (the serving
and six one-shot task definitions and the service). The real plan's counts must match;
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
| 5 | Protect the image (`keep-` tag); task definitions at 0 tasks | this plan | plan refuses anything else |
| 6 | `bootstrap` (restricted RDS administrator), `load-seed`, `boundary`, `test-users` | this plan | each exit code; logs hold no value |
| 7 | One serving task: Fargate volume ownership, readiness, verified TLS, the load balancer's path; browser sign-in, an answer, sign-out on the HTTPS URL | this plan | records naming commit, digest, configuration |
| 8 | Lifecycle: teardown and recreate within the budget (secret generations, snapshot label, kept images) | this plan | the recreate succeeds |
| 9 | Teardown at the end of the lifetime; residual costs as above | this plan | nothing left but what you choose to keep |

**Rollback and recovery limits.** No earlier release is a rollback target
(ROLLBACK_DECISION.md); a bad release is fixed forward. ECS rolls back a failed
deployment by itself. The database restores from an automated backup to a new instance
(point in time, 7 days); the first staging cycle measures it. One Single-AZ instance
means no failover is tested in this stage.

## Inputs needed (nothing is assumed)

| Input | Notes |
|---|---|
| AWS account and an **IAM Identity Center profile** you have logged in to (`aws configure sso`, then `aws sso login --profile <name>`); tell me the profile name | never access keys or passwords in chat |
| Region, and data residency | the estimate is refreshed for it |
| Monthly budget for this stage, and its lifetime | budgets alert, they do not cap |
| Alert recipients and the resource owner's name | for budget and alert email |
| Staging hostname, and who controls its DNS (a Route 53 zone id, or you create two records by hand) | |
| Testers' networks (CIDRs) | never `0.0.0.0/0` |
| A final-snapshot label convention (a date is enough) | for teardown |
| Later, each its own approval: identity-provider registration and role mapping; feed samples and source contract; Bedrock model, destinations and token/dollar caps; service, freshness, RTO/RPO, audit-mode and retention targets | |
