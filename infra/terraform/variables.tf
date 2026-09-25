variable "region" {
  description = "AWS region. Bedrock model availability varies by region."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Name tag and prefix for every resource."
  type        = string
  default     = "pharma-analytics-copilot"
}

variable "instance_type" {
  description = <<-EOT
    Graviton, because the workload is PostgreSQL plus a mostly-idle Python
    process and t4g is materially cheaper than t3 for the same memory.
    2 GiB is enough to load 2,000,000 rows; 1 GiB is not.
  EOT
  type        = string
  default     = "t4g.small"
}

variable "disk_gb" {
  description = "Root volume. The dataset plus indexes plus generated CSVs."
  type        = number
  default     = 30
}

variable "key_name" {
  description = "Existing EC2 key pair name for SSH. Administration can also go through SSM."
  type        = string
}

variable "my_ip" {
  description = "Your address in CIDR form, e.g. 203.0.113.4/32. SSH is limited to it."
  type        = string
  validation {
    condition     = can(cidrnetmask(var.my_ip))
    error_message = "my_ip must be CIDR, for example 203.0.113.4/32 -- not a bare address."
  }
}

variable "repo_url" {
  description = "Git URL the instance clones on first boot."
  type        = string
  default     = "https://github.com/patibandlavenkatamanideep/pharma-analytics-copilot.git"
}

variable "bedrock_model_id" {
  description = "The planner model the instance is permitted to invoke."
  type        = string
  default     = "anthropic.claude-opus-4-5-20251101-v1:0"
}

variable "bedrock_inference_profile" {
  description = "Dated Anthropic models are invoked through a cross-region inference profile."
  type        = string
  default     = "us.anthropic.claude-opus-4-5-20251101-v1:0"
}
