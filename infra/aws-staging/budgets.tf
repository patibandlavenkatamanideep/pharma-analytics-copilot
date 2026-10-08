# Notifications, not a stop: AWS keeps spending past a budget. The
# application's own limits (per-user quotas, the evaluation runner's token
# caps) bound model spend per call; these tell the owner when the account's
# total moves.

resource "aws_budgets_budget" "account" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.alert_emails
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.alert_emails
  }
}

variable "model_budget_usd" {
  description = "Monthly budget for Amazon Bedrock alone (the live-evaluation and staging model spend). 0: none."
  type        = number
  default     = 0
}

resource "aws_budgets_budget" "bedrock" {
  count        = var.model_budget_usd > 0 ? 1 : 0
  name         = "${var.name}-bedrock"
  budget_type  = "COST"
  limit_amount = tostring(var.model_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.alert_emails
  }
}
