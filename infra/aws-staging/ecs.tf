# One release image, run five ways: the serving service, and one-shot tasks to
# bootstrap the database, migrate it, load data and set test credentials. Every
# container: the image by digest, user 10001, read-only root filesystem, every
# Linux capability dropped, and no secret in its plain environment.

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_cloudwatch_log_group" "task" {
  for_each          = toset(["app", "jobs"])
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

locals {
  # No digest yet (the first apply): no task definition and no service.
  deployable = var.image_digest != null
  image      = local.deployable ? "${aws_ecr_repository.app.repository_url}@${var.image_digest}" : null
  db_ca      = "/etc/ssl/certs/rds-global-bundle.pem"

  # Every connection libpq makes verifies the RDS certificate.
  db_env = [
    { name = "PAC_ENVIRONMENT", value = "cloud" },
    { name = "PAC_DB_HOST", value = aws_db_instance.this.address },
    { name = "PAC_DB_PORT", value = "5432" },
    { name = "PAC_DB_NAME", value = "pharma_analytics" },
    { name = "PGSSLMODE", value = "verify-full" },
    { name = "PGSSLROOTCERT", value = local.db_ca },
  ]
  app_env = concat(local.db_env, [
    { name = "PAC_LLM_PROVIDER", value = var.llm_provider },
    { name = "PAC_BEDROCK_REGION", value = var.region },
    { name = "PAC_BEDROCK_MODEL_ID", value = var.bedrock_model_id },
    { name = "PAC_COOKIE_SECURE", value = "true" },
    # Only the load balancer can connect (security groups), so the client
    # address it forwards is trusted from the VPC range and from nowhere else.
    # Untrusted, every client is the load balancer, and 20 failed sign-ins lock
    # everyone out (evidence/probes/client_address_behind_proxy.py).
    { name = "FORWARDED_ALLOW_IPS", value = var.vpc_cidr },
    ], var.enable_observability ? [
    { name = "PAC_OTEL_ENDPOINT", value = "http://127.0.0.1:4318" },
    ] : [], var.llm_input_usd_per_mtok != null && var.llm_output_usd_per_mtok != null ? [
    { name = "PAC_LLM_INPUT_USD_PER_MTOK", value = tostring(var.llm_input_usd_per_mtok) },
    { name = "PAC_LLM_OUTPUT_USD_PER_MTOK", value = tostring(var.llm_output_usd_per_mtok) },
    ] : [], var.llm_spend_limit_usd != null ? [
    { name = "PAC_LLM_SPEND_LIMIT_USD", value = tostring(var.llm_spend_limit_usd) },
    ] : [], var.oidc_issuer != null ? [
    { name = "PAC_OIDC_ENABLED", value = "true" },
    { name = "PAC_OIDC_ISSUER", value = var.oidc_issuer },
    { name = "PAC_OIDC_CLIENT_ID", value = var.oidc_client_id },
    { name = "PAC_OIDC_REDIRECT_URI", value = "https://${var.hostname}/api/auth/oidc/callback" },
  ] : [])

  serving_secrets = concat([for r in ["auth", "exec", "scoped"] : {
    name      = "PAC_DB_${upper(r)}_PASSWORD"
    valueFrom = aws_secretsmanager_secret.db_role[r].arn
    }], var.oidc_issuer != null && var.oidc_confidential_client ? [{
    name      = "PAC_OIDC_CLIENT_SECRET"
    valueFrom = aws_secretsmanager_secret.oidc_client.arn
  }] : [])
  all_role_secrets = [for r in local.db_roles : {
    name      = "PAC_DB_${upper(r)}_PASSWORD"
    valueFrom = aws_secretsmanager_secret.db_role[r].arn
  }]

  hardened = {
    user                   = "10001"
    readonlyRootFilesystem = true
    privileged             = false
    linuxParameters = {
      capabilities       = { drop = ["ALL"], add = [] }
      initProcessEnabled = true
    }
  }

  logs = { for k in ["app", "jobs"] : k => {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.task[k].name
      awslogs-region        = var.region
      awslogs-stream-prefix = k
    }
  } }

  # The one-shot tasks: name => [command, extra environment, extra secrets].
  jobs = {
    # RDS's administrator: password from the RDS-managed secret, through
    # libpq's PGPASSWORD; the DSN carries no password.
    bootstrap = {
      command = ["python", "scripts/bootstrap_db.py", "--no-env"]
      env = [{ name = "PAC_ADMIN_DSN", value = join(" ", [
        "host=${aws_db_instance.this.address}", "port=5432", "dbname=postgres",
        "user=${aws_db_instance.this.username}", "sslmode=verify-full", "sslrootcert=${local.db_ca}",
      ]) }]
      secrets = [{ name = "PGPASSWORD", valueFrom = "${aws_db_instance.this.master_user_secret[0].secret_arn}:password::" }]
    }
    migrate = {
      command = ["python", "scripts/migrate.py"]
      env     = []
      secrets = []
    }
    load-seed = {
      command = ["python", "scripts/load_data.py", "--mode", "seed"]
      env     = []
      secrets = []
    }
    # The full synthetic dataset: generated into the image's one volume.
    load-full = {
      command = ["sh", "-c", "python schema/generate_data.py && python scripts/load_data.py --mode full"]
      env     = []
      secrets = []
    }
    # Disposable identities from the test-users secret; prints counts only.
    test-users = {
      command = ["python", "-c", join("\n", [
        "import json, os",
        "from app.auth.identity import set_credential",
        "users = json.loads(os.environ['PAC_TEST_USERS'])",
        "for uid, pw in users.items():",
        "    set_credential(uid, pw)",
        "print(f'credentials set for {len(users)} test users')",
      ])]
      env     = []
      secrets = [{ name = "PAC_TEST_USERS", valueFrom = aws_secretsmanager_secret.test_users.arn }]
    }
    # The dataset the serving task will publish: the full synthetic load, at
    # its known size, or this exits 1. Prints counts only.
    dataset-check = {
      command = ["python", "-c", join("\n", [
        "from app.llm.planner import OfflinePlanner",
        "from app.pipeline import Pipeline",
        "d = Pipeline(OfflinePlanner()).current_dataset()",
        "rows = d['row_counts']",
        "print({'dataset': d['dataset_id'], 'mode': d['load_mode'], 'rows': rows})",
        "ok = d['load_mode'] == 'full' and rows.get('sales') == 2000000 and rows.get('organizations') == 40000",
        "raise SystemExit(0 if ok else 1)",
      ])]
      env     = []
      secrets = []
    }
    # The security boundary, after provisioning or a restore.
    boundary = {
      command = ["python", "-c", join("\n", [
        "from app.db import verify_runtime_role_safety as v",
        "p = v()",
        "print(p or 'boundary intact')",
        "raise SystemExit(1 if p else 0)",
      ])]
      env     = []
      secrets = []
    }
  }
}

# What the tasks run must exist in the repository and be protected from
# routine expiry (ecr.tf): checked at every plan that deploys it. Named by
# var.name, the repository's name, not by the resource: a data source that
# depends on a resource with pending changes is read only at apply, after
# the check should have stopped the plan.
data "aws_ecr_image" "deployed" {
  count           = local.deployable ? 1 : 0
  repository_name = var.name
  image_digest    = var.image_digest
}

resource "aws_ecs_task_definition" "app" {
  count                    = local.deployable ? 1 : 0
  family                   = "${var.name}-app"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.app_cpu
  memory                   = var.app_memory
  execution_role_arn       = aws_iam_role.execution["app"].arn
  task_role_arn            = aws_iam_role.task["app"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  lifecycle {
    precondition {
      condition     = var.oidc_issuer == null || var.oidc_client_id != null
      error_message = "oidc_issuer needs oidc_client_id."
    }
    # A live model is called by every user, worker and task: never without
    # the application's allowance and the rates that price it.
    precondition {
      condition = var.llm_provider != "bedrock" || (var.enable_bedrock && var.llm_spend_limit_usd != null
      && var.llm_input_usd_per_mtok != null && var.llm_output_usd_per_mtok != null)
      error_message = "llm_provider = \"bedrock\" needs enable_bedrock, llm_spend_limit_usd and both llm_*_usd_per_mtok rates."
    }
    precondition {
      condition     = anytrue([for tag in data.aws_ecr_image.deployed[0].image_tags : startswith(tag, "keep-")])
      error_message = "The image_digest image must carry a keep- tag, so routine expiry of all but the last 20 images does not reach it while it is deployed (README.md, \"Deploy\")."
    }
    # A refusal, not a warning: a check block only warns, and the plan went on
    # to run the collector by tag (tests/plan.tftest.hcl).
    precondition {
      condition     = !var.enable_observability || can(regex("@sha256:[0-9a-f]{64}$", coalesce(var.collector_image, "")))
      error_message = "With enable_observability, collector_image must be pinned by digest (image@sha256:...)."
    }
  }

  container_definitions = jsonencode(concat([merge(local.hardened, {
    name         = "app"
    image        = local.image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.app_env
    secrets      = local.serving_secrets
    # uvicorn lets in-flight requests finish for 65 s after SIGTERM; Fargate
    # allows at most 120 s before SIGKILL.
    stopTimeout = 120
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
    logConfiguration = local.logs["app"]
    })], var.enable_observability ? [merge(local.hardened, {
    name             = "collector"
    image            = var.collector_image
    essential        = false
    command          = ["--config=env:PAC_COLLECTOR_CONFIG"]
    environment      = [{ name = "PAC_COLLECTOR_CONFIG", value = local.collector_config }]
    logConfiguration = local.logs["app"]
  })] : []))
}

resource "aws_ecs_task_definition" "job" {
  for_each                 = local.deployable ? local.jobs : {}
  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution["jobs"].arn
  task_role_arn            = aws_iam_role.task["jobs"].arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # The dataset generator writes here; the image declares it a volume owned
  # by the application user. Task storage, gone with the task.
  dynamic "volume" {
    for_each = each.key == "load-full" ? [1] : []
    content {
      name = "generated"
    }
  }

  ephemeral_storage {
    size_in_gib = 21
  }

  container_definitions = jsonencode([merge(local.hardened, {
    name             = each.key
    image            = local.image
    essential        = true
    command          = each.value.command
    environment      = concat(local.db_env, each.value.env)
    secrets          = concat(local.all_role_secrets, each.value.secrets)
    mountPoints      = each.key == "load-full" ? [{ sourceVolume = "generated", containerPath = "/app/schema/generated", readOnly = false }] : []
    logConfiguration = local.logs["jobs"]
  })])
}

resource "aws_ecs_service" "app" {
  count                              = local.deployable ? 1 : 0
  name                               = "${var.name}-app"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.app[0].arn
  desired_count                      = var.app_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 120
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = local.task_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = !local.nat
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.https]
}
