# Optional (enable_observability): the application's OTLP export goes to a
# collector beside it, which writes metrics to Amazon Managed Service for
# Prometheus; the repository's alert rules run there and alerts go to an SNS
# topic the owner subscribes to. Traces stay counts-only (debug exporter)
# until a trace backend is chosen, as in deploy/observability.

resource "aws_prometheus_workspace" "this" {
  count = var.enable_observability ? 1 : 0
  alias = var.name
}

resource "aws_prometheus_rule_group_namespace" "alerts" {
  count        = var.enable_observability ? 1 : 0
  name         = "pac-alerts"
  workspace_id = aws_prometheus_workspace.this[0].id
  data         = file("${path.module}/../../deploy/observability/alerts.yml")
}

# alerts.yml's TelemetryPipelineDown tests `up` for a scraped collector. With
# remote write nothing is scraped, so here a counter every process reports
# from start (pac.persistence.failures, zero-started) is the heartbeat.
resource "aws_prometheus_rule_group_namespace" "staging" {
  count        = var.enable_observability ? 1 : 0
  name         = "pac-staging"
  workspace_id = aws_prometheus_workspace.this[0].id
  data = yamlencode({
    groups = [{
      name = "pac-staging"
      rules = [{
        alert       = "TelemetryNotArriving"
        expr        = "absent_over_time(pac_persistence_failures_total[10m])"
        annotations = { summary = "No application process has reported metrics for 10 minutes" }
      }]
    }]
  })
}

resource "aws_sns_topic" "alerts" {
  count             = var.enable_observability ? 1 : 0
  name              = "${var.name}-alerts"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "alerts" {
  for_each  = var.enable_observability ? toset(var.alert_emails) : toset([])
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = each.value
}

data "aws_iam_policy_document" "alerts_topic" {
  count = var.enable_observability ? 1 : 0
  statement {
    actions   = ["sns:Publish", "sns:GetTopicAttributes"]
    resources = [aws_sns_topic.alerts[0].arn]
    principals {
      type        = "Service"
      identifiers = ["aps.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_prometheus_workspace.this[0].arn]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  count  = var.enable_observability ? 1 : 0
  arn    = aws_sns_topic.alerts[0].arn
  policy = data.aws_iam_policy_document.alerts_topic[0].json
}

resource "aws_prometheus_alert_manager_definition" "this" {
  count        = var.enable_observability ? 1 : 0
  workspace_id = aws_prometheus_workspace.this[0].id
  definition = yamlencode({
    alertmanager_config = {
      route     = { receiver = "owner", group_by = ["alertname"] }
      receivers = [{ name = "owner", sns_configs = [{ topic_arn = aws_sns_topic.alerts[0].arn, sigv4 = { region = var.region } }] }]
    }
  })
}

locals {
  amp_endpoint = one(aws_prometheus_workspace.this[*].prometheus_endpoint)
  collector_config = yamlencode({
    extensions = { sigv4auth = { region = var.region, service = "aps" } }
    receivers  = { otlp = { protocols = { http = { endpoint = "127.0.0.1:4318" } } } }
    processors = {
      memory_limiter = { check_interval = "1s", limit_mib = 200 }
      batch          = { timeout = "5s" }
    }
    exporters = {
      prometheusremotewrite = {
        endpoint = "${coalesce(local.amp_endpoint, "https://unset.invalid/")}api/v1/remote_write"
        auth     = { authenticator = "sigv4auth" }
      }
      debug = { verbosity = "basic" }
    }
    service = {
      extensions = ["sigv4auth"]
      telemetry  = { metrics = { level = "none" } }
      pipelines = {
        metrics = { receivers = ["otlp"], processors = ["memory_limiter", "batch"], exporters = ["prometheusremotewrite"] }
        traces  = { receivers = ["otlp"], processors = ["memory_limiter", "batch"], exporters = ["debug"] }
      }
    }
  })
}
