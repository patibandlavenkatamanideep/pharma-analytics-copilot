# Deployment

Two paths. Both produce a public HTTPS URL with the database unreachable from
the internet. Pick based on what you can operate confidently, not on which
sounds more impressive.

| | AWS App Runner + RDS | Single VM + Compose |
|---|---|---|
| Public HTTPS | managed, automatic | Caddy/nginx + Let's Encrypt |
| Database | RDS PostgreSQL, private subnet | container on a private network |
| Availability | multi-AZ possible | **single host — no guarantee** |
| Time to first deploy | ~30 min | ~15 min |
| Cost | ~$50–80/mo | ~$10–20/mo |

The README prefers AWS but does not require it.

---

## A. AWS: App Runner + RDS

`deploy_aws.sh` performs these steps; they are written out so the deployment can
be reproduced or audited by hand.

### 1. Build and push the image

```bash
export AWS_REGION=us-east-1
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export REPO=pharma-analytics-copilot

aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" || true
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com"

# App Runner runs x86_64; build for it explicitly from an Apple Silicon machine.
docker build --platform linux/amd64 -t "$REPO" .
docker tag "$REPO:latest" "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest"
docker push "$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO:latest"
```

### 2. RDS PostgreSQL, not publicly accessible

```bash
aws rds create-db-instance \
  --db-instance-identifier pac-db \
  --db-instance-class db.t4g.small \
  --engine postgres --engine-version 16 \
  --allocated-storage 40 --storage-type gp3 \
  --master-username pacadmin \
  --master-user-password "$RDS_MASTER_PASSWORD" \
  --backup-retention-period 7 \
  --no-publicly-accessible \
  --db-subnet-group-name "$PRIVATE_SUBNET_GROUP" \
  --vpc-security-group-ids "$DB_SECURITY_GROUP"
```

`--no-publicly-accessible` is the important flag. The security group should
allow 5432 **only** from the App Runner VPC connector's security group — not
from `0.0.0.0/0`, and not from your laptop.

### 3. Secrets

Never in the image, never in environment variables in the console.

```bash
aws secretsmanager create-secret --name pac/db \
  --secret-string "$(jq -n \
      --arg o "$PAC_DB_OWNER_PASSWORD" --arg a "$PAC_DB_AUTH_PASSWORD" \
      --arg e "$PAC_DB_EXEC_PASSWORD"  --arg s "$PAC_DB_SCOPED_PASSWORD" \
      '{owner:$o, auth:$a, exec:$e, scoped:$s}')"
```

Reference them from the App Runner service configuration.

### 4. Initialise the database

Bootstrap, load and provisioning need direct database access, so run them from
somewhere inside the VPC — a bastion, a one-off ECS task, or a CloudShell
session with a VPC endpoint:

```bash
python3 scripts/bootstrap_db.py --admin-dsn "postgresql://pacadmin:***@$RDS_ENDPOINT/postgres"
python3 schema/generate_data.py
python3 scripts/load_data.py --mode full
python3 scripts/provision_logins.py --demo
```

The generated CSVs are ~250 MB; generating them inside the VPC is faster than
uploading.

### 5. App Runner service

- Source: the ECR image above, port `8000`
- Health check path: `/ready` (not `/health` — `/ready` is false until a
  dataset is published, so a half-loaded deployment never takes traffic)
- VPC connector: the private subnets that can reach RDS
- Instance role: `AmazonBedrockFullAccess`, or a policy scoped to
  `bedrock:InvokeModel` on the specific model ARNs
- Environment: `PAC_LLM_PROVIDER=bedrock`, `PAC_COOKIE_SECURE=true`,
  `PAC_ENVIRONMENT=cloud`, `PAC_DB_HOST=<rds endpoint>`

App Runner provides the HTTPS certificate and the public URL.

### 6. Verify

```bash
./infra/smoke.sh https://<service>.awsapprunner.com
```

---

## B. Single VM + Docker Compose

Any provider. Fastest reliable route to a public URL.

```bash
ssh user@host
git clone <repo> && cd pharma-analytics-copilot
cp .env.example .env && $EDITOR .env      # set the passwords

docker compose up -d db
docker compose run --rm app python scripts/bootstrap_db.py --drop
docker compose run --rm app python schema/generate_data.py
docker compose run --rm app python scripts/load_data.py --mode full
docker compose run --rm app python scripts/provision_logins.py --demo
docker compose up -d app
```

Then put a TLS terminator in front. Caddy is two lines and handles renewal:

```
analytics.example.com {
    reverse_proxy localhost:8000
}
```

The database port is deliberately not published in `compose.yaml`, so
PostgreSQL is reachable only over the compose network.

**Say this honestly in any write-up:** a single host has no availability
guarantee. A restart or a host failure is downtime. That is an acceptable
trade-off for an assessment and should not be described as production-ready.

---

## Status of these artifacts

**The image builds and runs.** It is built and exercised on every push by the
`image` job in `.github/workflows/ci.yml`, which asserts that it:

- **refuses to start when no database is reachable** — startup verifies the
  database security boundary, so a mis-provisioned deployment fails loudly
  rather than quietly serving unrestricted data;
- can **provision the database from inside the image**, proving it carries
  working scripts and not just the server;
- serves `/health` 200 against a real database;
- returns `/ready` **503** until a dataset is published, so a container with a
  half-loaded refresh never takes traffic;
- serves the built UI from the same origin, refuses `/api/me` with 401, and
  does not run as root.

`compose.yaml` itself is still **unverified** — no run of `docker compose up`
has happened — but the image it builds is the one CI exercises.

**Nothing has been deployed.** There is no AWS infrastructure, no ECR
repository, no RDS instance and no public URL. The deployment steps below are
written from the AWS documentation and have not been executed.

---

## Operational notes

**Restart safety.** Data lives in the `pgdata` volume (Compose) or RDS storage.
Sessions and conversations survive because they are in PostgreSQL, not memory.
Verify by restarting and signing in again without re-provisioning.

**Bedrock.** Two gates that are not code:
1. Anthropic **use-case details** submitted for the account, once. Until then
   every call fails with `Model use case details have not been submitted`.
2. The right model id. Dated releases need an inference profile prefix (`us.`
   or `global.`); bare ids return `on-demand throughput isn't supported`.

With `PAC_LLM_PROVIDER=offline` the whole application still runs, using the
deterministic planner. That is the right setting if the model gate is unresolved
at deploy time — it degrades the natural-language layer, not the data, the
security or the correctness.

**Rollback.** Push the previous image tag; App Runner keeps prior revisions. The
database is unchanged by a rollback because migrations are additive.

**What to check before calling it done.** `infra/smoke.sh` covers it: `/ready`
returns a dataset id, each role logs in and sees a different scope, a non-Exec
revenue question returns volume with the restriction stated, and a market-share
question carries its data-quality warning.
