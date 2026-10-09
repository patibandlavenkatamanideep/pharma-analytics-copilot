# Plans of this module with a mocked AWS provider: no credentials, no API
# call, nothing created. They check what each configuration would create and
# what it refuses; they do not show that AWS accepts it.
#
#   cd infra/aws-staging && terraform test

mock_provider "aws" {
  # The provider checks that a policy is a JSON object even in a mocked plan.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["us-east-1a", "us-east-1b"] }
  }
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  override_data {
    target = data.aws_partition.current
    values = { partition = "aws" }
  }
}

# Known at plan time, so the image reference can be read.
override_resource {
  target          = aws_ecr_repository.app
  override_during = plan
  values = {
    repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/pac-staging"
    arn            = "arn:aws:ecr:us-east-1:123456789012:repository/pac-staging"
  }
}

variables {
  region             = "us-east-1"
  hostname           = "staging.example.com"
  allowed_cidrs      = ["198.51.100.0/24"]
  monthly_budget_usd = 150
  alert_emails       = ["owner@example.com"]
}

run "foundation_without_an_image" {
  command = plan

  assert {
    condition     = length(aws_ecs_task_definition.app) == 0 && length(aws_ecs_service.app) == 0
    error_message = "with no image_digest, nothing that runs the image is planned"
  }
  assert {
    condition     = length(aws_ecs_task_definition.job) == 0
    error_message = "no one-shot task definitions without an image"
  }
  assert {
    condition     = aws_ecr_repository.app.image_tag_mutability == "IMMUTABLE"
    error_message = "the repository to push to exists, with immutable tags"
  }
  assert {
    condition     = length(aws_nat_gateway.this) == 0 && length(aws_prometheus_workspace.this) == 0
    error_message = "the low-cost shape has no NAT gateway and no Prometheus workspace"
  }
}

run "deployable_at_zero_tasks" {
  command = plan

  variables {
    image_digest = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
  }

  assert {
    condition     = length(aws_ecs_task_definition.app) == 1 && aws_ecs_service.app[0].desired_count == 0
    error_message = "the serving service starts at zero tasks until data is loaded"
  }
  assert {
    condition     = toset(keys(aws_ecs_task_definition.job)) == toset(["bootstrap", "migrate", "load-seed", "load-full", "test-users", "boundary"])
    error_message = "the six one-shot task definitions"
  }
  assert {
    condition     = output.image == "123456789012.dkr.ecr.us-east-1.amazonaws.com/pac-staging@sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    error_message = "every task runs the image by repository and digest, never a tag"
  }
  assert {
    condition     = aws_ecs_service.app[0].network_configuration[0].assign_public_ip == true
    error_message = "public_ip egress: tasks get an address (inbound only from the load balancer)"
  }
}

run "private_tasks_behind_nat" {
  command = plan

  variables {
    image_digest = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    egress_mode  = "nat"
  }

  assert {
    condition     = length(aws_nat_gateway.this) == 1 && aws_ecs_service.app[0].network_configuration[0].assign_public_ip == false
    error_message = "nat egress: one NAT gateway, tasks without public addresses"
  }
}

run "failover_shape" {
  command = plan

  variables {
    image_digest      = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    app_desired_count = 2
    db_multi_az       = true
  }

  assert {
    condition     = aws_ecs_service.app[0].desired_count == 2 && aws_db_instance.this.multi_az == true
    error_message = "two tasks and a Multi-AZ database"
  }
}

run "the_whole_internet_is_refused" {
  command = plan

  variables {
    allowed_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.allowed_cidrs]
}

run "a_tag_is_not_a_digest" {
  command = plan

  variables {
    image_digest = "latest"
  }

  expect_failures = [var.image_digest]
}

run "sso_needs_a_client_id" {
  command = plan

  variables {
    image_digest = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    oidc_issuer  = "https://idp.example.com"
  }

  expect_failures = [aws_ecs_task_definition.app]
}

# Observability with a collector named by tag: the README and the variable say
# the collector must be pinned by digest. Planning must stop, not warn.
run "an_unpinned_collector_is_refused" {
  command = plan

  variables {
    image_digest         = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    enable_observability = true
    collector_image      = "otel/opentelemetry-collector-contrib:0.162.0"
  }

  expect_failures = [aws_ecs_task_definition.app]
}
