# Restricted AWS staging

Terraform for one staging environment of pharma-analytics-copilot: the
release image on ECS Fargate behind an HTTPS load balancer, a private RDS
PostgreSQL 16, and one-shot tasks that bootstrap, migrate and load it. It is
separate from the earlier demo (`../terraform`, an EC2 host destroyed on
2026-10-02), from `main`'s deployment and from anything shared.

**Status: validated, never planned or applied.** `terraform validate` and
`fmt` pass with Terraform 1.16.5 and the AWS provider 6.68.0
(`.terraform.lock.hcl`). A plan needs AWS credentials for the target account,
and nothing in this repository has used any. The status of each check, and
what is still owed, is in [docs/AWS_STAGING.md](../../docs/AWS_STAGING.md).

## What it creates

| Area | Resources | Notes |
|---|---|---|
| Network (`network.tf`) | VPC `10.40.0.0/16`; 2 public and 2 private subnets in two zones; internet gateway; route tables; the default security group emptied; VPC flow logs to CloudWatch (30 days) | `egress_mode = "nat"` adds one NAT gateway and its address |
| Access (`security_groups.tf`) | load balancer: 443 and 80 from `allowed_cidrs` only; serving tasks: 8000 from the load balancer only; one-shot tasks: no inbound; database: 5432 from the two task groups only | egress: 443 (AWS endpoints) and 5432 (database) |
| Load balancer (`alb.tf`) | internet-facing ALB, TLS 1.3 policy, HTTP → HTTPS redirect, invalid headers dropped, `X-Forwarded-For` appended; target group on `/ready`; ACM certificate for `hostname` | Route 53 records only when `route53_zone_id` is given |
| Database (`rds.tf`) | RDS PostgreSQL 16, `db.t4g.small`, 20 GB gp3 encrypted, private, `rds.force_ssl = 1`, scram passwords, no statement text in logs; RDS-managed administrator password; 7-day backups; deletion protection; final snapshot | `db_multi_az` for the failover test |
| Registry (`ecr.tf`) | ECR repository, immutable tags, scan on push, last 20 images kept | |
| Secrets (`secrets.tf`) | containers for the four database roles, test users and the OIDC client secret — no values | values from `seed-secrets.sh` |
| Compute (`ecs.tf`) | cluster; log groups (30 days); serving task definition and service (rolling, circuit breaker with rollback); one-shot task definitions `bootstrap`, `migrate`, `load-seed`, `load-full`, `test-users`, `boundary` | only once `image_digest` is set |
| Identity (`iam.tf`) | execution roles (serving: three serving-role secrets only; jobs: all database secrets); task roles (serving: Bedrock and Prometheus only when enabled); GitHub OIDC provider and a publish-only role | |
| Cost alerts (`budgets.tf`) | monthly account budget (50/80/100 % actual, 100 % forecast); optional Bedrock budget | alerts only — AWS does not stop at a budget |
| Observability (`observability.tf`) | with `enable_observability`: Prometheus workspace, the repository's alert rules plus `TelemetryNotArriving`, alert manager → SNS → owner email; a collector beside the app | collector image pinned by digest |

Every container runs as user 10001 with a read-only root filesystem, every
Linux capability dropped, no privilege, an init process, and the image by
digest. Only `/app/schema/generated` is writable (the image declares it a
volume owned by the application user); the `load-full` task mounts task
storage there.

## Decisions the owner makes first

| Input | Variable | Why it has no default |
|---|---|---|
| Account and region | provider credentials; `region` | residency, model availability, price |
| Hostname, and the Route 53 zone if any | `hostname`, `route53_zone_id` | the certificate and the URL |
| Testers' networks | `allowed_cidrs` (`0.0.0.0/0` is refused) | staging is not public |
| Monthly budget and alert addresses | `monthly_budget_usd`, `alert_emails` | spend notifications |
| Release image | `image_digest` (after publishing) | deployment is by digest only |
| Model use | `llm_provider`, `enable_bedrock`, `model_budget_usd`, `llm_*_usd_per_mtok` | spend; off by default |
| Single sign-on | `oidc_issuer`, `oidc_client_id`, `oidc_confidential_client` | needs a registration with the provider |
| Observability | `enable_observability`, `collector_image` | about $47 a month for two tasks (cost) |

## Identity and state

* **People** use expiring credentials from IAM Identity Center
  (`aws sso login --profile <p>`); nobody creates an access key.
* **CI** publishes images with `.github/workflows/publish-staging.yml`, which
  assumes the publish role through GitHub OIDC, only from a job in the
  `staging` environment of this repository. That role can push to its
  repository and read what it pushed, nothing else: it cannot deploy, run a
  task, read a secret or pass a role.
* **Deploying** is a Terraform apply of a reviewed plan, by the owner.
* **State** holds identifiers, never a password (RDS generates the
  administrator's; `seed-secrets.sh` writes the rest). Keep it in an S3
  bucket in the same account with versioning, default encryption, public
  access blocked and access limited to the owner's role; uncomment the
  `backend "s3"` block in `versions.tf` (`use_lockfile = true`).

## Create

Each step's plan is read before it is applied. The commands assume
`export AWS_PROFILE=<sso profile>` and a `staging.tfvars` holding the
decisions above (it holds no secret).

1. **Initialise.** `terraform init -backend-config=...` then
   `terraform validate`.
2. **Certificate, if no Route 53 zone is given.** The validation record
   exists only once the certificate does:
   `terraform apply -var-file=staging.tfvars -target=aws_acm_certificate.this`,
   then create the CNAME from
   `terraform state show aws_acm_certificate.this` (`domain_validation_options`)
   at your DNS provider. The next step waits until the certificate is issued.
3. **Foundation.** `terraform plan -var-file=staging.tfvars -out=foundation.plan`
   with no `image_digest`, then `terraform apply foundation.plan`. Creates
   everything except task definitions and the service. Point `hostname` at
   `load_balancer_dns_name` if Route 53 does not.
4. **Secret values.** `./seed-secrets.sh` (database roles). It prints names
   and "set" only, and leaves a container that already holds a value alone.
5. **Publish the image.** In GitHub, create the environment `staging` with a
   required reviewer, set its variables `AWS_REGION`, `AWS_ACCOUNT_ID`,
   `PAC_PUBLISH_ROLE_ARN` (output `github_publish_role_arn`) and
   `PAC_ECR_REPOSITORY_URL` (output `ecr_repository_url`), and run
   *publish-staging* on the release commit. It builds for linux/amd64 without
   a cache, runs the same scan and hardened image journeys as CI, pushes, and
   writes the registry digest to the run summary. The image a laptop
   qualified is not this image (a different build and, on Apple silicon, a
   different architecture): this run's gates are what qualify the digest.
6. **Task definitions.** Set `image_digest` to that digest (keep
   `app_desired_count = 0`), plan, apply.
7. **Database.** Run the one-shot tasks in order, each to exit code 0:
   `bootstrap` (roles, database, migrations, graph store, logins — as the RDS
   administrator, which gets only `SET ROLE pac_owner`), `load-seed` or
   `load-full`, `boundary`, and, once user ids are chosen,
   `./seed-secrets.sh <user_id> ...` then `test-users`:

   ```sh
   net=$(terraform output -json run_task_network)
   aws ecs run-task --cluster pac-staging --launch-type FARGATE \
     --task-definition pac-staging-bootstrap \
     --network-configuration "awsvpcConfiguration={subnets=[$(echo "$net" | jq -r '.subnets|join(",")')],securityGroups=[$(echo "$net" | jq -r '.security_groups[0]')],assignPublicIp=$(echo "$net" | jq -r .assign_public_ip)}" \
     --query 'tasks[0].taskArn' --output text
   aws ecs wait tasks-stopped --cluster pac-staging --tasks <arn>
   aws ecs describe-tasks --cluster pac-staging --tasks <arn> \
     --query 'tasks[0].containers[0].exitCode'
   ```

   Their output is in the `/ecs/pac-staging/jobs` log group; none prints a
   value.
8. **Serve.** `app_desired_count = 1`, plan, apply;
   `aws ecs wait services-stable --cluster pac-staging --services pac-staging-app`;
   then `curl -fsS https://<hostname>/ready`.

## Update (a new release)

Publish (step 5), then plan with the new `image_digest`: the plan must show
only the task definitions and the service changing. If the release adds
migrations, run `migrate` first. Migrations only move forward and converge;
whether the release being replaced keeps serving on the migrated schema is
measured per pair of releases (`evidence/probes/upgrade_compatibility.py
--previous <sha>`) — run it for the pair before applying. Apply; ECS replaces
tasks keeping 100 % healthy and rolls back by itself if new tasks fail their
health checks. That leaves Terraform's state naming the newer task
definition: the next plan shows the difference.

## Roll back, fix forward, restore

As [docs/ROLLBACK_DECISION.md](../../docs/ROLLBACK_DECISION.md) decides: a
previous digest is a rollback target only if it was qualified and has no
known access or correctness defect (`evidence/ledger.json`); no release
before the current candidate is one. The default is a forward fix through
the same gates and a new digest. A lost or damaged database is restored from
an automated backup of the upgraded database (point in time, within 7 days)
to a new instance and repointed (`docs/RUNBOOK.md` §8, managed database).
Containment without a code change: `oidc_issuer = null` (password sign-in
remains) or `llm_provider = "offline"`.

## Failover and replica tests

Set `db_multi_az = true` and `app_desired_count = 2`, apply, then
`aws rds reboot-db-instance --db-instance-identifier pac-staging --force-failover`
and measure what users see. Put both back afterwards: the second task and
the standby double the cost of the parts they double (cost).

## Tear down

1. `app_desired_count = 0`, apply.
2. `deletion_protection = false`, apply.
3. Delete the images (`force_delete` is off so that a destroy cannot remove
   a released image unnoticed): `aws ecr batch-delete-image ...`, or keep the
   repository by removing it from state first.
4. `terraform destroy`. RDS takes the final snapshot `pac-staging-final`;
   secrets enter a 7-day recovery window; log groups are deleted.
5. Delete the final snapshot once nothing needs it (it is billed until
   then), and the state bucket last.

If the GitHub OIDC provider is shared with other roles, create this stack
with `create_github_oidc_provider = false` so the destroy leaves it.

## Cost

[cost/ESTIMATE.md](cost/ESTIMATE.md), generated from AWS's public price list
(us-east-1, offers dated 2026-09-11 to 2026-10-07) by
[cost/estimate.py](cost/estimate.py): about **$96 a month** for low-cost
staging (one task, Single-AZ, no Prometheus, no model calls) and about
**$213 a month** while the replica and failover tests run (two tasks,
Multi-AZ, Prometheus). The biggest items are the serving task, the database
and the load balancer; public IPv4 addresses are $3.65 each a month.
Private tasks behind a NAT gateway add about $34; model calls cost about $30
per 1,000 questions. Refresh with `estimate.py extract --offers <dir>` from
newly downloaded offer files.

## Static scan

`trivy config` (0.58.1) on this directory: no finding beyond these, each
accepted for staging.

| Check | Severity | Why accepted |
|---|---|---|
| AVD-AWS-0053 load balancer exposed to the internet | HIGH | it must be reachable by testers; its security group admits only `allowed_cidrs` (never `0.0.0.0/0`, enforced by validation and `tests/unit/test_staging_infra.py`) |
| AVD-AWS-0176 RDS IAM authentication off | MEDIUM | the application authenticates with scram passwords from Secrets Manager; IAM tokens would need code changes |
| AVD-AWS-0017, -0033, -0098 customer-managed KMS keys | LOW | AWS-managed keys encrypt logs, images and secrets; a key per service is $1 a month each and adds key policy to review |
| AVD-AWS-0034 Container Insights | LOW | cost; the application's own metrics go to Prometheus |
| AVD-AWS-0133 Performance Insights | LOW | cost; the database's own statistics views remain available |

## Not verified until it runs on AWS

* that the plan and apply succeed in the chosen account and region, and the
  `log_min_error_statement` parameter is accepted;
* that Fargate gives the image's volume to user 10001 (`load-full`);
* that the RDS administrator's `GRANT pac_owner TO CURRENT_USER WITH SET`
  behaves as in the local emulation (`evidence/probes/restricted_admin_provisioning.py`);
* TLS against the real RDS certificate chain (locally: a private CA,
  `evidence/probes/db_tls_verify_full.py`);
* client addresses through the real load balancer (locally: uvicorn behind a
  local proxy, `evidence/probes/client_address_behind_proxy.py`);
* the alert rules' metric names after remote write, and `TelemetryNotArriving`.
