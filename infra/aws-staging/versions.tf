# Restricted AWS staging for pharma-analytics-copilot: one release image on ECS
# Fargate behind an HTTPS load balancer, a private RDS PostgreSQL, and
# one-shot tasks for migrations and data. Separate from the earlier demo
# (../terraform, an EC2 host, destroyed on 2026-10-02) and from anything
# shared. Read README.md before planning: nothing here has been applied.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State holds resource identifiers, never a password: database credentials
  # are generated and stored by RDS and by infra/aws-staging/seed-secrets.sh
  # in Secrets Manager. Keep state in an encrypted, versioned S3 bucket that
  # only the owner's deploying session can read (README.md, "State"); local state is for
  # a first validation only.
  # backend "s3" {
  #   bucket       = "<state bucket>"
  #   key          = "pharma-analytics-copilot/staging.tfstate"
  #   region       = "<region>"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "pharma-analytics-copilot"
      Environment = "staging"
      ManagedBy   = "terraform/infra/aws-staging"
    }
  }
}
