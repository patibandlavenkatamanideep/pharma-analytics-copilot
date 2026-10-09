# Decisions the owner makes before any plan: they have no defaults on purpose.

variable "region" {
  description = "AWS region for every resource. Bedrock model availability and data residency depend on it."
  type        = string
}

variable "hostname" {
  description = "The staging hostname, e.g. staging.example.com. An ACM certificate is requested for it."
  type        = string
}

variable "route53_zone_id" {
  description = "Hosted zone that holds the hostname, to create the alias and the certificate validation records. Null: create them by hand from the outputs."
  type        = string
  default     = null
}

variable "public_sign_in" {
  description = "The owner's decision of 9 October 2026: \"Anyone may reach the sign-in page, but only accounts I approve may use the application.\" True: the load balancer admits HTTPS (and the redirect from HTTP) from every address; the tasks and the database stay reachable only from it, there is no public sign-up, and accounts come from the reviewers job. False (the default): only allowed_cidrs."
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "Source ranges allowed to reach the load balancer (the testers' networks). Never the whole internet."
  type        = list(string)
  validation {
    condition = length(var.allowed_cidrs) > 0 && alltrue([
      for c in var.allowed_cidrs : can(cidrnetmask(c)) && c != "0.0.0.0/0"
    ])
    error_message = "allowed_cidrs must be a non-empty list of CIDR ranges, and 0.0.0.0/0 is not allowed."
  }
}

variable "image_digest" {
  description = "Registry manifest digest of the qualified release image, sha256:<64 hex>, as the push reported it (a local image's digest can differ). Null on the first apply: everything but the task definitions and the service is created, so the repository exists to push to. Deployment is by digest only; a tag names whatever was pushed last."
  type        = string
  default     = null
  validation {
    condition     = var.image_digest == null || can(regex("^sha256:[0-9a-f]{64}$", var.image_digest))
    error_message = "image_digest must be a full sha256:<64 hex> registry digest."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the account's staging spend. Alerts only: AWS does not stop spending at a budget."
  type        = number
}

variable "alert_emails" {
  description = "Owner-approved addresses for budget and alert notifications. Each address must confirm its subscription."
  type        = list(string)
}

variable "github_repository" {
  description = "owner/name of the repository whose workflow may publish images."
  type        = string
  default     = "patibandlavenkatamanideep/pharma-analytics-copilot"
}

variable "github_environment" {
  description = "The GitHub environment a publishing workflow job must run in; the role trusts nothing else."
  type        = string
  default     = "staging"
}

variable "create_github_oidc_provider" {
  description = "An account has one GitHub OIDC provider. False: one exists; give its ARN."
  type        = bool
  default     = true
}

variable "github_oidc_provider_arn" {
  description = "Existing provider ARN when create_github_oidc_provider is false."
  type        = string
  default     = null
}

# Shape and cost. Defaults are the low-cost initial staging (README.md, "Cost").

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "pac-staging"
}

variable "vpc_cidr" {
  description = "Address range of the staging VPC. The application trusts X-Forwarded-For only from it (the load balancer)."
  type        = string
  default     = "10.40.0.0/16"
}

variable "egress_mode" {
  description = "public_ip: tasks in public subnets with a public address and no inbound rule except from the load balancer (lowest cost). nat: tasks in private subnets behind one NAT gateway."
  type        = string
  default     = "public_ip"
  validation {
    condition     = contains(["public_ip", "nat"], var.egress_mode)
    error_message = "egress_mode is public_ip or nat."
  }
}

variable "app_desired_count" {
  description = "Serving tasks. 0 until the database is bootstrapped and a dataset is loaded (a task with no data never becomes ready); 1 for initial staging; 2 for the multi-task, rolling-deployment and failover tests."
  type        = number
  default     = 0
}

variable "app_cpu" {
  description = "Fargate CPU units per serving task (two uvicorn workers)."
  type        = number
  default     = 1024
}

variable "app_memory" {
  description = "Fargate memory (MiB) per serving task."
  type        = number
  default     = 2048
}

variable "db_instance_class" {
  description = "RDS class. db.t4g.small: about 225 connections, enough for two serving tasks (2 workers x 25 each) plus jobs. db.t4g.micro is too small for two tasks."
  type        = string
  default     = "db.t4g.small"
}

variable "db_engine_version" {
  description = "PostgreSQL major version on RDS. 16 matches what the suites ran on (16.14)."
  type        = string
  default     = "16"
}

variable "db_allocated_storage_gb" {
  description = "gp3 storage. The full synthetic dataset with indexes is about 1.2 GB."
  type        = number
  default     = 20
}

variable "db_multi_az" {
  description = "A standby in a second zone. False for initial staging; true only for the failover test, and say so in any availability claim."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Automated backups and point-in-time recovery window."
  type        = number
  default     = 7
}

variable "final_snapshot_label" {
  description = "Names the snapshot RDS takes when the database is destroyed: <name>-final-<label>. Set it before each teardown to a value never used before (a date, 20261009), so an earlier final snapshot that is kept does not block the next teardown. No default: the operator chooses it."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,30}$", var.final_snapshot_label))
    error_message = "final_snapshot_label is lowercase letters, digits and hyphens (at most 31)."
  }
}

variable "secret_generation" {
  description = "Part of every secret's name. Deleted secrets keep their names for 7 days; to recreate the stack inside that window without restoring them, move to the next generation (g2, g3, ...)."
  type        = string
  default     = "g1"
  validation {
    condition     = can(regex("^g[0-9]+$", var.secret_generation))
    error_message = "secret_generation is g followed by a number."
  }
}

variable "deletion_protection" {
  description = "Protects the database from deletion. Set false (and apply) only as the first step of teardown."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for every task's log group."
  type        = number
  default     = 30
}

variable "llm_provider" {
  description = "offline: no model is called (no spend). bedrock: needs enable_bedrock and an approved budget."
  type        = string
  default     = "offline"
  validation {
    condition     = contains(["offline", "bedrock"], var.llm_provider)
    error_message = "llm_provider is offline or bedrock."
  }
}

variable "enable_bedrock" {
  description = "Allow the serving task to invoke the one model profile below."
  type        = bool
  default     = false
}

variable "bedrock_model_id" {
  description = "The inference profile the planner calls (PAC_BEDROCK_MODEL_ID); its region and routing must suit the data-residency decision."
  type        = string
  default     = "us.anthropic.claude-opus-4-5-20251101-v1:0"
}

variable "llm_input_usd_per_mtok" {
  description = "Contracted input rate (USD per million tokens) for the cost metric, pac.llm.cost. Null: no cost is estimated. The 2026-10-07 us-east-1 price list gives 5.50 for a us. (geographic) Opus 4.5 profile and 5.00 for global."
  type        = number
  default     = null
  validation {
    condition     = var.llm_input_usd_per_mtok == null || try(var.llm_input_usd_per_mtok > 0, false)
    error_message = "llm_input_usd_per_mtok must be above zero: a zero price makes every call free to the allowance."
  }
}

variable "llm_output_usd_per_mtok" {
  description = "Contracted output rate (USD per million tokens); 27.50 for a us. Opus 4.5 profile and 25.00 for global in the same price list."
  type        = number
  default     = null
  validation {
    condition     = var.llm_output_usd_per_mtok == null || try(var.llm_output_usd_per_mtok > 0, false)
    error_message = "llm_output_usd_per_mtok must be above zero: a zero price makes every call free to the allowance."
  }
}

variable "llm_spend_limit_usd" {
  description = "The website's model allowance in USD (PAC_LLM_SPEND_LIMIT_USD): one limit for every user, worker and task, enforced by the application before each model call. Required with llm_provider = \"bedrock\"; evaluation runs have their own cap, so set this to the model budget minus that cap."
  type        = number
  default     = null
  validation {
    condition     = var.llm_spend_limit_usd == null || try(var.llm_spend_limit_usd >= 0, false)
    error_message = "llm_spend_limit_usd must be zero or more (zero refuses every call)."
  }
}

# Single sign-on, once the owner has registered a client with the identity
# provider (docs/RUNBOOK.md, "Single sign-on"). Null issuer: SSO is off and
# local password sign-in is the only method.
variable "oidc_issuer" {
  description = "The issuer exactly as the provider publishes it."
  type        = string
  default     = null
}

variable "oidc_client_id" {
  description = "The registered client's id."
  type        = string
  default     = null
}

variable "oidc_confidential_client" {
  description = "True when the client has a secret: it is read from the oidc-client-secret container, which must hold a value first."
  type        = bool
  default     = false
}

variable "enable_observability" {
  description = "An Amazon Managed Service for Prometheus workspace with the alert rules, alert routing to SNS, and a collector beside the application."
  type        = bool
  default     = false
}

variable "collector_image" {
  description = "OpenTelemetry Collector image for the sidecar, pinned by digest (required when enable_observability)."
  type        = string
  default     = null
}
