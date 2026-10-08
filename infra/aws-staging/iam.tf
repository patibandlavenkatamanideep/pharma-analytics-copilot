# Four roles for the tasks, one for the publishing workflow. The serving task's
# execution role can read only the serving roles' secrets: the owner and the
# RDS administrator secrets are readable only by the one-shot tasks, so a
# serving task never holds the owner credential (the application refuses to
# start with it in cloud mode).

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  # A cross-region inference profile, and the model it routes to in each region.
  bedrock_base_model = replace(var.bedrock_model_id, "/^(us|eu|apac|global)\\./", "")
  bedrock_resources = [
    "arn:${local.partition}:bedrock:${var.region}:${local.account}:inference-profile/${var.bedrock_model_id}",
    "arn:${local.partition}:bedrock:*::foundation-model/${local.bedrock_base_model}",
  ]
}

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

# --- execution roles: pull the image, write logs, inject secrets -------------

data "aws_iam_policy_document" "execution" {
  for_each = {
    app = [for r in ["auth", "exec", "scoped"] : aws_secretsmanager_secret.db_role[r].arn]
    jobs = concat([for r in local.db_roles : aws_secretsmanager_secret.db_role[r].arn],
    [aws_db_instance.this.master_user_secret[0].secret_arn, aws_secretsmanager_secret.test_users.arn])
  }

  statement {
    sid       = "PullFromItsRepository"
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.app.arn]
  }
  statement {
    sid       = "RegistryToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "WriteItsLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.task[each.key].arn}:*"]
  }
  statement {
    sid       = "ReadItsSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = concat(each.value, each.key == "app" ? [aws_secretsmanager_secret.oidc_client.arn] : [])
  }
}

resource "aws_iam_role" "execution" {
  for_each           = toset(["app", "jobs"])
  name               = "${var.name}-${each.key}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "execution" {
  for_each = toset(["app", "jobs"])
  name     = "execution"
  role     = aws_iam_role.execution[each.key].id
  policy   = data.aws_iam_policy_document.execution[each.key].json
}

# --- task roles: what the running code may call -------------------------------

resource "aws_iam_role" "task" {
  for_each           = toset(["app", "jobs"])
  name               = "${var.name}-${each.key}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "app_task" {
  dynamic "statement" {
    for_each = var.enable_bedrock ? [1] : []
    content {
      sid       = "InvokeThePlannerModel"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = local.bedrock_resources
    }
  }
  dynamic "statement" {
    for_each = var.enable_observability ? [1] : []
    content {
      sid       = "WriteMetrics"
      actions   = ["aps:RemoteWrite"]
      resources = [aws_prometheus_workspace.this[0].arn]
    }
  }
}

# Only when the serving task has something to call: otherwise it gets no
# permission at all.
resource "aws_iam_role_policy" "app_task" {
  count  = var.enable_bedrock || var.enable_observability ? 1 : 0
  name   = "app"
  role   = aws_iam_role.task["app"].id
  policy = data.aws_iam_policy_document.app_task.json
}

# --- the publishing workflow, by GitHub OIDC ---------------------------------
# It pushes images and nothing else. What runs changes only by a Terraform
# apply of a reviewed plan with a new image_digest, by the owner with an
# expiring session (README.md, "Deploy"): CI can add an image to the
# repository (tags are immutable) but cannot deploy, run a task, read a
# secret or pass a role.

resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_github_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = []
}

locals {
  github_oidc_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.github_oidc_provider_arn
}

data "aws_iam_policy_document" "publish_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # One repository, and only a job running in the protected environment.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "publish" {
  name                 = "${var.name}-github-publish"
  assume_role_policy   = data.aws_iam_policy_document.publish_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "publish" {
  statement {
    sid       = "RegistryToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushToItsRepository"
    actions = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages",
    "ecr:DescribeImageScanFindings"]
    resources = [aws_ecr_repository.app.arn]
  }
}

resource "aws_iam_role_policy" "publish" {
  name   = "publish"
  role   = aws_iam_role.publish.id
  policy = data.aws_iam_policy_document.publish.json
}
