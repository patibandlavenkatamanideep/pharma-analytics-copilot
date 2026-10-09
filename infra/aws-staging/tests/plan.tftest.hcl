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

# The deployed image exists and is protected from routine expiry, unless a
# run says otherwise.
override_data {
  target = data.aws_ecr_image.deployed
  values = { image_tags = ["d266c32", "keep-20261009-d266c32"] }
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
  # Test values throughout: none is an owner's decision.
  final_snapshot_label = "test1"
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
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.alb_https_public) == 0 && length(aws_vpc_security_group_ingress_rule.alb_http_public) == 0
    error_message = "by default, nothing is open to the whole internet"
  }
  assert {
    condition     = aws_db_instance.this.final_snapshot_identifier == "pac-staging-final-test1"
    error_message = "the final snapshot is named by the operator's label"
  }
  assert {
    condition     = aws_secretsmanager_secret.db_role["owner"].name == "pac-staging/g1/db/owner" && aws_secretsmanager_secret.test_users.name == "pac-staging/g1/test-users"
    error_message = "secret names carry the generation"
  }
  assert {
    condition     = jsondecode(aws_ecr_lifecycle_policy.app.policy).rules[0].selection.tagPrefixList == ["keep-"] && jsondecode(aws_ecr_lifecycle_policy.app.policy).rules[0].rulePriority == 1
    error_message = "keep-* images are selected first, so the last-20 rule cannot expire them"
  }
}

run "a_new_secret_generation_renames_every_secret" {
  command = plan

  variables {
    secret_generation = "g2"
  }

  assert {
    condition     = alltrue([for s in aws_secretsmanager_secret.db_role : startswith(s.name, "pac-staging/g2/")]) && startswith(aws_secretsmanager_secret.oidc_client.name, "pac-staging/g2/")
    error_message = "every secret moves to the new generation"
  }
}

run "an_image_without_a_keep_tag_is_not_deployed" {
  command = plan

  variables {
    image_digest = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
  }

  override_data {
    target = data.aws_ecr_image.deployed
    values = { image_tags = ["d266c32"] }
  }

  expect_failures = [aws_ecs_task_definition.app]
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
    condition     = toset(keys(aws_ecs_task_definition.job)) == toset(["bootstrap", "migrate", "load-seed", "load-full", "dataset-check", "test-users", "reviewers", "boundary"])
    error_message = "the eight one-shot task definitions"
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

# A live model with no allowance: every user, worker and task would call it
# without a spending limit.
run "a_live_model_needs_an_allowance" {
  command = plan

  variables {
    image_digest            = "sha256:d266c32ae92e1a0590b7cd3dee9bc0a9521019bd01e17d47dcbb228cb4961443"
    llm_provider            = "bedrock"
    enable_bedrock          = true
    llm_input_usd_per_mtok  = 5.5
    llm_output_usd_per_mtok = 27.5
  }

  expect_failures = [aws_ecs_task_definition.app]
}

# A zero price makes every call free to the application's allowance.
run "a_zero_model_price_is_refused" {
  command = plan

  variables {
    llm_input_usd_per_mtok = 0
  }

  expect_failures = [var.llm_input_usd_per_mtok]
}

# The owner's decision: anyone may reach the sign-in page. Only the load
# balancer's listeners open; the tasks and the database do not.
run "a_public_sign_in_page_opens_only_the_load_balancer" {
  command = plan

  variables {
    public_sign_in = true
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.alb_https_public[0].cidr_ipv4 == "0.0.0.0/0" && aws_vpc_security_group_ingress_rule.alb_https_public[0].from_port == 443
    error_message = "HTTPS to the load balancer from anywhere"
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.alb_http_public) == 1
    error_message = "and the HTTP redirect"
  }
}

# The publish role trusts one repository's subject; a wildcard would let any
# repository's token in.
run "a_wildcard_github_subject_is_refused" {
  command = plan

  variables {
    github_subject_prefix = "repo:patibandlavenkatamanideep@*/*"
  }

  expect_failures = [var.github_subject_prefix]
}
